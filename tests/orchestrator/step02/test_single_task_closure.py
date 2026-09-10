# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · S2-01 / S2-02 / S2-06 / S2-07 on the deterministic fixture provider:
Mission → Planner → Commit → Reserve → Attempt → BaseAgent (real tools, real pytest
in a child process) → Result Envelope → Verifier layers → repair Attempt → Commit →
Mission judged."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step, proposal_step

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.commit_service import MissionSpec
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig

SEED = {
    "parse_kv.py": "def parse_kv(text):\n    raise NotImplementedError\n",
    "tests/test_parse_kv.py": (
        "from parse_kv import parse_kv\n\n\n"
        "def test_basic():\n    assert parse_kv('a=1;b=2') == {'a': '1', 'b': '2'}\n\n\n"
        "def test_empty():\n    assert parse_kv('') == {}\n"
    ),
}
GOOD = "def parse_kv(text):\n    return dict(p.split('=', 1) for p in text.split(';') if p)\n"
BAD = "def parse_kv(text):\n    return dict(p.split('=', 1) for p in text.split(';'))\n"  # fails test_empty

PROPOSAL = {
    "goal": "实现 parse_kv(text) -> dict 并通过 tests/test_parse_kv.py",
    "rationale": "Mission 只有这一件工作；通过给定测试即满足成功条件",
    "success_criteria": ["pytest:tests/test_parse_kv.py", "file:parse_kv.py"],
    "verification_policy": ["format_check", "rule_check", "critic_review", "code_test"],
    "allowed_tools": ["workspace_read_file", "workspace_write_file", "workspace_list", "run_tests"],
    "budget": {"max_tokens": 50000, "max_attempts": 2},
    "priority": 1.0,
    "root_goal": "实现字符串解析函数并通过测试",
}


def spec(key="m1", **overrides):
    base = dict(
        goal="在隔离工作区实现字符串解析函数 parse_kv，并通过给定测试",
        success_criteria=("pytest:tests/test_parse_kv.py", "实现应处理空字符串"),
        tenant_id="tenant-a",
        idempotency_key=key,
        allowed_tools=(
            "workspace_read_file",
            "workspace_write_file",
            "workspace_list",
            "run_tests",
        ),
        budget=Budget(max_tokens=200_000, max_attempts=3),
        workspace_seed=SEED,
    )
    base.update(overrides)
    return MissionSpec(**base)


def worker_script(code):
    return [
        ("workspace_list", {}),
        ("workspace_read_file", {"path": "tests/test_parse_kv.py"}),
        ("workspace_write_file", {"path": "parse_kv.py", "content": code}),
        ("run_tests", {"path": "tests/test_parse_kv.py"}),
        envelope_step(
            summary="实现了 parse_kv",
            artifacts=["parse_kv.py"],
            claims=["parse_kv 通过 tests/test_parse_kv.py"],
        ),
    ]


def config(tmp_path, **overrides):
    base = dict(
        evidence_root=Path(tmp_path) / "evidence", max_concurrency=1, test_timeout_seconds=60
    )
    base.update(overrides)
    return OrchestratorConfig(**base)


def test_s2_01_and_s2_02_repair_then_pass(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": worker_script(BAD) + worker_script(GOOD),
            "critic": [
                critic_step(verdict="PASS", criteria_met=True),
                critic_step(verdict="PASS", criteria_met=True),
            ],
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec())
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            assert final.stop_reason == "verification_passed"
            task = store.list_tasks(mission.id)[0]
            assert task.status is TaskStatus.COMPLETED and task.attempt_count == 2
            attempts = store.list_attempts(task.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT,
                AttemptStatus.COMPLETED,
            ]
            assert attempts[1].retry_of == attempts[0].id
            assert attempts[1].feedback and "code_test" in attempts[1].feedback[0]
            first = store.find_result_for_attempt(attempts[0].id)
            assert first.verdict == "FAIL"
            layers = {v["layer"]: v["status"] for v in store.list_verifications(first.envelope.id)}
            assert layers["code_test"] == "FAIL" and layers["critic_review"] == "PASS"
            second = store.find_result_for_attempt(attempts[1].id)
            assert second.verdict == "PASS"
            assert task.accepted_result_id == second.envelope.id
            # real artifact with the real hash of the accepted file
            artifact = store.get_artifact(task.accepted_artifacts[0])
            assert artifact.path == "parse_kv.py"
            produced = (
                orchestrator.config.workspaces_root / attempts[1].id / "parse_kv.py"
            ).read_text()
            assert produced == GOOD
            # events of §16.2 exist exactly once where they should
            types = [e.type for e in store.list_events(mission.id)]
            for expected in (
                "MissionCreated",
                "TaskCommitted",
                "AttemptStarted",
                "ResultSubmitted",
                "VerificationFailed",
                "VerificationPassed",
                "TaskCompleted",
                "MissionSuccessJudged",
                "MissionCompleted",
            ):
                assert expected in types, expected
            assert types.count("AttemptStarted") == 2 and types.count("BudgetReserved") >= 4
            # both attempts, the planner and both critics settled against the Mission
            with store.transaction():
                report = orchestrator.commit.ledger.costs_report(mission.id)
            settled = {r["subject_id"]: r["state"] for r in report["reservations"]}
            assert all(state == "SETTLED" for state in settled.values()), settled
            assert len(settled) == 5  # planner + 2 attempts + 2 critics
            mission_account = next(a for a in report["accounts"] if a["scope"] == "mission")
            assert mission_account["reserved_tokens"] == 0 and mission_account["settled_tokens"] > 0
            assert mission_account["unpriced_settlements"] == 5
            # deterministic provider never saw the planner twice
            assert provider.by_role == {"planner": 1, "worker": 10, "critic": 2}

    asyncio.run(case())


def test_s2_06_max_attempts_stops_with_reason(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": worker_script(BAD) + worker_script(BAD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m6"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED
            assert final.stop_reason == "max_attempts_reached"
            task = store.list_tasks(mission.id)[0]
            assert task.status is TaskStatus.FAILED and task.attempt_count == 2
            assert len(final.final_report["completed_parts"]) == 2
            assert all(p["verdict"] == "FAIL" for p in final.final_report["completed_parts"])
            types = [e.type for e in store.list_events(mission.id)]
            assert (
                types.count("VerificationFailed") == 2
                and "TaskFailed" in types
                and "MissionFailed" in types
            )

    asyncio.run(case())


def test_s2_07_invalid_and_forged_envelopes_are_rejected(tmp_path):
    forged = envelope_step(
        summary="伪造",
        artifacts=["parse_kv.py"],
        claims=["x"],
        override=lambda e: {**e, "attempt_id": "someone-else:attempt-9"},
    )
    provider = RoleScriptedProvider(
        {
            "planner": [
                proposal_step({**PROPOSAL, "budget": {"max_tokens": 50000, "max_attempts": 3}})
            ],
            "worker": [
                ("workspace_write_file", {"path": "parse_kv.py", "content": GOOD}),
                "这不是一个信封，只是自然语言。",  # attempt 1: no block
                ("workspace_write_file", {"path": "parse_kv.py", "content": GOOD}),
                forged,  # attempt 2: forged identity
                ("workspace_write_file", {"path": "parse_kv.py", "content": GOOD}),
                envelope_step(
                    summary="引用不存在的产物", artifacts=["ghost.py"], claims=["x"]
                ),  # attempt 3
            ],
            "critic": [],
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m7"))
            await orchestrator.run()
            store = orchestrator.store
            task = store.list_tasks(mission.id)[0]
            attempts = store.list_attempts(task.id)
            assert [a.status for a in attempts] == [AttemptStatus.RETRY_WAIT] * 3
            reasons = [a.failure["reason"] for a in attempts]
            assert reasons == ["envelope_invalid"] * 3
            assert "block_missing" in attempts[0].failure["error"]
            assert "identity" in attempts[1].failure["error"]
            assert "ghost.py" in attempts[2].failure["error"]
            # no formal result / claim was ever written
            assert all(store.find_result_for_attempt(a.id) is None for a in attempts)
            assert store.list_mission_claims(mission.id) == []
            assert store.count_events(mission.id, "ResultRejected") == 3
            final = store.get_mission(mission.id)
            assert (
                final.status is MissionStatus.FAILED and final.stop_reason == "max_attempts_reached"
            )

    asyncio.run(case())


def test_s2_03_replayed_mission_is_the_same_mission(tmp_path):
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": worker_script(GOOD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)],
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            first = await orchestrator.submit_mission(spec("m3"))
            second = await orchestrator.submit_mission(spec("m3"))
            assert (
                first.id == second.id
                and orchestrator.store.count_events(first.id, "MissionCreated") == 1
            )
            await orchestrator.run()
            assert orchestrator.store.get_mission(first.id).status is MissionStatus.COMPLETED
            snapshot = orchestrator.store.snapshot(first.id)
            assert len(snapshot["tasks"]) == 1 and len(snapshot["attempts"]) == 1
            json.dumps(snapshot)  # serialisable for final_state.json

    asyncio.run(case())


def test_d21_mission_judgment_runs_its_own_critic_when_the_task_policy_had_none(tmp_path):
    """A real Planner may choose a policy without critic_review; a free-text Mission
    criterion still gets an independent judge at judgment time (D21)."""

    from fixtures_provider import critic_step as _critic

    proposal = {**PROPOSAL, "verification_policy": ["format_check", "rule_check", "code_test"]}
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(proposal)],
            "worker": worker_script(GOOD),
            "critic": [_critic(verdict="PASS", criteria_met=True)],
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m-d21"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log
            judged = final.final_report["success_criteria"]
            assert [j["judge"] for j in judged] == ["code_test", "critic_review"]
            assert all(j["met"] for j in judged)
            assert provider.by_role["critic"] == 1  # the judge ran exactly once
            layers = {
                v["layer"]: v["status"]
                for v in store.list_verifications(
                    store.list_tasks(mission.id)[0].accepted_result_id
                )
            }
            assert layers["critic_review"] == "NOT_REQUIRED"  # not part of the Task policy
            with store.transaction():
                report = orchestrator.commit.ledger.costs_report(mission.id)
            assert any(
                r["subject_id"].endswith(":judge:1")
                and r["state"] == "SETTLED"  # step 3: judge subject
                for r in report["reservations"]
            )

    asyncio.run(case())

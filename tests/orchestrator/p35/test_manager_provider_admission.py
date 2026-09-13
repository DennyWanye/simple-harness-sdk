# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Manager evidence is historical; its own durable turn authorizes each request.

These controls use real Orchestrator/SDK execution and both SQLite stores with
in-process transports. No network, manufactured admission errors or guard bypass.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from graph_helpers7 import drive_to_running, graph_service, node, spec
from test_provider_budget_guard import ActualProvider, Counter, create_bound, grants

from agent_orchestrator.contracts import AttemptStatus, Budget, MissionStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import Reservation, mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.agent_worker import AgentBridge, user_message_json
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.model_router import RuntimeProfile
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard
from agent_orchestrator.testing.fixtures import (
    MODEL,
    RoleScriptedProvider,
    critic_step,
    envelope_step,
    graph_change_step,
    graph_proposal_step,
    package_of,
)
from simple_harness.agents import AgentConfig, AgentRuntimePorts, build_agent_runtime
from simple_harness.agents.ports import AllowAllAuthorization
from simple_harness.contracts import RunId


def test_failed_verification_manager_handoff_decision_and_retry_use_actual_runtime(tmp_path):
    async def exercise():
        manager_origins = []

        def manager(request):
            package = package_of(request)
            origin = orch.store.get_attempt(package["trigger"]["attempt_id"])
            assert origin.status is AttemptStatus.RETRY_WAIT
            assert origin.failure["reason"] == "verification_failed"
            manager_origins.append(origin.id)
            return graph_change_step(
                [{"op": "set_role", "task_id": origin.task_id, "role": "simplifier"}]
            )(request)

        def assessed(request):
            reads = [json.loads(m.content) for m in request.messages if str(m.role) == "tool"]
            content = reads[-1]["value"]["content"]
            passed = content == "complete\n"
            return critic_step(
                verdict="PASS" if passed else "FAIL", criteria_met=passed,
                blocker=None if passed else "partial content requires another approach",
            )(request)

        def worker(content):
            return [
                ("workspace_write_file", {"path": "a.md", "content": content}),
                envelope_step(summary=content, artifacts=["a.md"], claims=[content]),
            ]

        provider = RoleScriptedProvider({
            "planner": [graph_proposal_step([node(
                "A", tokens=120_000,
                verification_policy=["format_check", "rule_check", "critic_review"],
            )])],
            "worker": worker("partial\n"),
            "simplifier": worker("complete\n"),
            "critic": [("workspace_read_file", {"path": "a.md"}), assessed] * 2,
            "manager": [manager],
        })
        profile = RuntimeProfile(
            "default", provider, MODEL, max_concurrent_model_calls=1,
            default_max_output_tokens=1000, max_output_tokens_ceiling=1000,
        )
        config = OrchestratorConfig(
            evidence_root=tmp_path, max_concurrency=1, max_concurrent_model_calls=1,
            candidates_per_task=1, manager_after_failures=1, max_manager_rounds=1,
        )
        async with Orchestrator(
            config, profiles={"default": profile}, provider_token_estimator=Counter(1000),
        ) as orch:
            mission = await orch.submit_mission(spec(
                "manager-admission", success_criteria=("file:a.md",),
                budget=Budget(max_tokens=300_000, max_attempts=12),
            ))
            await asyncio.wait_for(orch.run(), 15)
            final = orch.store.get_mission(mission.id)
            assert final.status is MissionStatus.COMPLETED, orch.progress_log
            assert final.budget == mission.budget
            task = orch.store.list_tasks(mission.id)[0]
            assert task.budget.max_tokens == 120_000
            attempts = orch.store.list_attempts(task.id)
            assert [a.status for a in attempts] == [
                AttemptStatus.RETRY_WAIT, AttemptStatus.COMPLETED,
            ]
            assert manager_origins == [attempts[0].id]
            assert provider.by_role["manager"] == 1
            events = orch.store.list_events(mission.id)
            failed = next(e for e in events if e.type == "VerificationFailed")
            requested = [e for e in events if e.type == "ManagementRequested"]
            decided = [e for e in events if e.type == "ManagementDecided"]
            changed = [e for e in events if e.type == "TaskGraphChanged"]
            assert len(requested) == len(decided) == len(changed) == 1
            assert failed.seq < requested[0].seq < changed[0].seq
            assert decided[0].payload["decision"] == "changed"
            intents = [i for i in orch.store.list_intents("SETTLED") if i.kind == "manager"]
            assert len(intents) == 1
            intent = intents[0]
            assert intent.subject_id == f"{mission.id}:manager:failures:{task.id}:1"
            assert intent.config["attempt_id"] == attempts[0].id
            rows = [r for r in grants(orch.commit) if r["subject_id"] == intent.subject_id]
            assert len(rows) == 1 and rows[0]["state"] == "SETTLED"
            assert rows[0]["actual_tokens"] == 150
            assert (rows[0]["agent_id"], rows[0]["turn_id"], rows[0]["intent_id"]) == (
                intent.agent_id, intent.expected_turn_id, intent.intent_id,
            )
            uow = orch.assembled.runtime.uow
            result = uow.read_agent_turn_result(intent.expected_turn_id)
            assert result is not None and result.result_json["error"] is None
            binding = uow.read_agent_binding(intent.agent_id)
            records = uow.list_provider_invocations(RunId(binding.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 1
            assert str(records[0].state) == "succeeded"
            assert records[0].invocation_id == rows[0]["invocation_id"]
            reservation = orch.commit.ledger.reservation(intent.subject_id)
            assert reservation["account_id"] == mission_account(mission.id)
            assert reservation["state"] == "SETTLED" and reservation["settled_tokens"] == 150
            assert len(grants(orch.commit)) == provider.calls
            assert all(r["state"] == "SETTLED" for r in grants(orch.commit))

    asyncio.run(exercise())


@asynccontextmanager
async def _runtime(tmp_path, *, blocked=False):
    commit, mission, tasks = graph_service(tmp_path, nodes=[node("A"), node("B")])
    guard = ProviderBudgetGuard(
        commit, owner="test-owner", estimator=Counter(100), max_slots=1,
        profile_slots={"agent.general": 1, "other": 1},
        price_tables={"agent.general": None, "other": None},
    )
    provider = ActualProvider(blocked=blocked)
    ports = AgentRuntimePorts(
        provider=provider, authorization=AllowAllAuthorization(),
        database_path=str(tmp_path / "execution.db"), provider_admission=guard,
        default_max_output_tokens=1000, max_output_tokens_ceiling=1000,
    )
    try:
        async with build_agent_runtime(ports) as runtime:
            parent = drive_to_running(commit, tasks["A"], owner="test-owner")
            parent = commit.mark_attempt_lost(parent.id, reason="controlled historical evidence")
            yield commit, mission, tasks, parent, guard, provider, runtime
    finally:
        commit.store.close()


async def _bind(commit, mission, task, parent, guard, runtime, *, key="one",
                kind="manager", overrides=None, agent_override=None, turn_override=None):
    agent = await runtime.create(
        AgentConfig(name=key, instructions="Answer briefly.", model_profile_ref="agent.general"),
        creation_key=key,
    )
    # Deliberately use a composite Manager-looking subject for every role. Only
    # durable kind/config/binding may decide whether the Attempt is historical.
    subject = f"{mission.id}:manager:failures:{task.id}:{key}"
    config = {
        "agent_config": agent.config.to_json(), "message": user_message_json("request"),
        "provider_admission_fingerprint": guard.fingerprint,
        "runtime_profile_id": "agent.general", "task_id": task.id,
        "attempt_id": parent.id,
        **(overrides or {}),
    }
    intent = commit.create_service_intent(
        kind=kind, subject_id=subject, mission_id=mission.id,
        account_id=mission_account(mission.id), creation_key=subject, input_id="attempt-input",
        input_hash="controlled-request", config=config, reservation=Reservation(4000, 0),
    )
    commit.claim_intent(intent.intent_id, owner="test-owner", lease_seconds=60)
    commit.record_agent_created(
        intent.intent_id, agent_id=agent_override or agent.agent_id,
        expected_turn_id=turn_override or agent.turn_id_for(intent.input_id),
    )
    return agent, intent


def _budget_rows(commit):
    return {
        table: [tuple(row) for row in commit.store.connection.execute(f"SELECT * FROM {table}")]
        for table in ("budget_accounts", "budget_reservations")
    }


@pytest.mark.parametrize("case", [
    "historical_manager", "manager_without_attempt", "live_critic", "plan", "mission_critic",
])
def test_composite_subject_uses_explicit_role_and_exact_runtime_binding(tmp_path, case):
    async def exercise():
        async with _runtime(tmp_path) as (commit, mission, tasks, parent, guard, provider, runtime):
            kind, overrides = "manager", {}
            if case == "manager_without_attempt":
                overrides["attempt_id"] = None
            elif case == "live_critic":
                parent = drive_to_running(
                    commit, tasks["B"], owner="test-owner", agent="critic-source",
                )
                kind = "critic"
                overrides["task_id"] = tasks["B"].id
            elif case in {"plan", "mission_critic"}:
                kind = "plan" if case == "plan" else "critic"
                overrides = {"task_id": None, "attempt_id": (
                    None if case == "plan" else f"{mission.id}-judge-final"
                )}
            agent, intent = await _bind(
                commit, mission, tasks["A"], parent, guard, runtime, kind=kind, overrides=overrides,
            )
            result = await agent.ask("request", input_id=intent.input_id, timeout=5)
            assert str(result.state) == "committed" and provider.calls == 1
            rows = grants(commit)
            assert len(rows) == 1 and rows[0]["state"] == "SETTLED"
            assert rows[0]["subject_id"] == intent.subject_id
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 1
            assert rows[0]["invocation_id"] == records[0].invocation_id
            facts = AgentBridge(runtime, unpriced=True).usage_facts(agent_id=agent.agent_id)
            with commit.store.transaction():
                commit.ledger.import_usage(
                    subject_id=intent.subject_id, mission_id=mission.id, facts=facts,
                )
                assert commit.ledger.settle(subject_id=intent.subject_id)["settled_tokens"] == 150

    asyncio.run(exercise())


@pytest.mark.parametrize("case", [
    "missing_task", "missing_attempt", "empty_attempt", "wrong_task", "wrong_mission",
    "wrong_agent", "wrong_turn", "wrong_contract", "wrong_profile", "wrong_pool",
    "terminal_critic", "cancelled_mission", "stopped_intent", "expired_lease",
])
def test_manager_exception_preserves_authority_pool_contract_and_cancel_guards(tmp_path, case):
    async def exercise():
        async with _runtime(tmp_path) as (commit, mission, tasks, parent, guard, provider, runtime):
            overrides = {}
            if case == "missing_task":
                overrides["task_id"] = None
            elif case in {"missing_attempt", "empty_attempt"}:
                overrides["attempt_id"] = "not-an-attempt" if case == "missing_attempt" else ""
            elif case == "wrong_task":
                overrides["task_id"] = tasks["B"].id
            elif case == "wrong_mission":
                foreign, _ = commit.create_mission(spec("foreign"))
                planning = commit.begin_planning(foreign.id)
                foreign_tasks, _ = commit.commit_task_graph(
                    foreign.id, TaskGraphProposal.from_json({"tasks": [node("F")]}),
                    base_version=planning.version, source={"planner": "controlled fixture"},
                )
                foreign_parent = drive_to_running(
                    commit, foreign_tasks[0], owner="test-owner", agent="foreign-agent",
                )
                overrides["attempt_id"] = foreign_parent.id
                overrides["task_id"] = foreign_parent.task_id
            elif case == "wrong_contract":
                overrides["provider_admission_fingerprint"] = "wrong-frozen-contract"
            elif case == "wrong_profile":
                overrides["runtime_profile_id"] = "other"
            agent, intent = await _bind(
                commit, mission, tasks["A"], parent, guard, runtime,
                kind="critic" if case == "terminal_critic" else "manager", overrides=overrides,
                agent_override="unrelated-agent" if case == "wrong_agent" else None,
                turn_override="unrelated-turn" if case == "wrong_turn" else None,
            )
            if case == "cancelled_mission":
                commit.cancel_mission(mission.id)
            elif case == "stopped_intent":
                commit.settle_intent(intent.intent_id, "FAILED")
            elif case == "expired_lease":
                # Expire just this service's pre-submit lease, without changing
                # the SDK clock, global budget or cancellation behavior.
                with commit.store.transaction():
                    commit.store.connection.execute(
                        "UPDATE dispatch_intents SET lease_expires_at=0 WHERE intent_id=?",
                        (intent.intent_id,),
                    )
            before = _budget_rows(commit)

            async def ask():
                return await agent.ask("request", input_id=intent.input_id, timeout=5)

            if case == "wrong_pool":
                other_ports = AgentRuntimePorts(
                    provider=ActualProvider(), authorization=AllowAllAuthorization(),
                    database_path=str(tmp_path / "other-execution.db"), provider_admission=guard,
                    default_max_output_tokens=1000,
                )
                async with build_agent_runtime(other_ports) as other:
                    acquire = guard.acquire

                    async def wrong_pool(**kwargs):
                        return await acquire(**{**kwargs, "uow": other.uow})

                    guard.acquire = wrong_pool
                    try:
                        result = await ask()
                    finally:
                        guard.acquire = acquire
            else:
                result = await ask()
            assert str(result.state) == "failed" and provider.calls == 0
            assert result.error["source_kind"] == "provider_admission"
            assert result.error["detail"]["reason_code"] == "authority_rejected"
            assert not result.error["retryable"]
            persisted = runtime.uow.read_agent_turn_result(result.turn_id)
            assert persisted.result_json["error"] == result.error
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 0
            assert grants(commit) == []
            assert _budget_rows(commit) == before

    asyncio.run(exercise())


def test_cancel_after_manager_grant_still_fences_physical_handoff(tmp_path):
    async def exercise():
        async with _runtime(tmp_path) as (commit, mission, tasks, parent, guard, provider, runtime):
            agent, intent = await _bind(commit, mission, tasks["A"], parent, guard, runtime)
            acquire = guard.acquire

            async def cancel_after_acquire(**kwargs):
                ticket = await acquire(**kwargs)
                assert grants(commit)[0]["state"] == "RESERVED"
                commit.cancel_mission(mission.id)
                return ticket

            guard.acquire = cancel_after_acquire
            result = await agent.ask("request", input_id=intent.input_id, timeout=5)
            assert str(result.state) == "failed" and provider.calls == 0
            rows = grants(commit)
            assert len(rows) == 1 and rows[0]["state"] == "RELEASED"
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 0

    asyncio.run(exercise())


def test_terminal_worker_cannot_use_manager_evidence_exception(tmp_path):
    async def exercise():
        async with _runtime(tmp_path) as (commit, _, tasks, _, guard, provider, runtime):
            agent, attempt, key = await create_bound(
                commit, tasks["B"], guard, runtime, "terminal-worker",
            )
            commit.mark_attempt_lost(attempt.id, reason="worker authority ended")
            before = _budget_rows(commit)
            result = await agent.ask("request", input_id=key, timeout=5)
            assert str(result.state) == "failed" and provider.calls == 0
            assert result.error["detail"]["reason_code"] == "authority_rejected"
            assert grants(commit) == [] and _budget_rows(commit) == before
            records = runtime.uow.list_provider_invocations(RunId(agent.run_id))
            assert len(records) == 1 and records[0].handoff_attempt == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_scope", ["mission", "turn"])
def test_manager_waits_for_real_slot_and_cancelled_waiter_never_hands_off(tmp_path, cancel_scope):
    async def exercise():
        async with _runtime(tmp_path, blocked=True) as (
            commit, mission, tasks, parent, guard, provider, runtime,
        ):
            first, first_intent = await _bind(
                commit, mission, tasks["A"], parent, guard, runtime, key="first",
            )
            second, second_intent = await _bind(
                commit, mission, tasks["A"], parent, guard, runtime, key="second",
            )
            running = asyncio.create_task(
                first.ask("request", input_id=first_intent.input_id, timeout=5),
            )
            waiting = None
            try:
                await asyncio.wait_for(provider.entered.wait(), 3)
                waiting = asyncio.create_task(second.ask(
                    "request", input_id=second_intent.input_id, timeout=5,
                ))

                async def queued():
                    while not guard.waiting_for_slot(
                        agent_id=second.agent_id,
                        turn_id=second.turn_id_for(second_intent.input_id),
                    ):
                        if waiting.done():
                            raise AssertionError("Manager stopped before entering actual slot wait")
                        await asyncio.sleep(0.001)

                await asyncio.wait_for(queued(), 3)
                assert provider.calls == 1 and len(grants(commit)) == 1
                if cancel_scope == "mission":
                    commit.cancel_mission(mission.id)
                else:
                    await second.cancel_turn(
                        second.turn_id_for(second_intent.input_id),
                        command_id="cancel-manager-waiter", wait_timeout=3,
                    )
                provider.allow.set()
                await asyncio.gather(running, waiting)
                assert provider.calls == 1 and len(grants(commit)) == 1
                assert grants(commit)[0]["state"] == "SETTLED"
                records = runtime.uow.list_provider_invocations(RunId(second.run_id))
                assert len(records) == 1 and records[0].handoff_attempt == 0
                assert not guard.waiting_for_slot(
                    agent_id=second.agent_id,
                    turn_id=second.turn_id_for(second_intent.input_id),
                )
            finally:
                provider.allow.set()
                await asyncio.gather(
                    *(job for job in (running, waiting) if job is not None),
                    return_exceptions=True,
                )

    asyncio.run(exercise())

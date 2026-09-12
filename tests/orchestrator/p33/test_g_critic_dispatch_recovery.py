# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real SDK dispatch gaps, cold library reopen and an unanswered Critic timeout.

Faults interrupt after durable creation or after actual submit, never invent SDK
turns/results. Cold recovery means a new runtime on the closed SQLite libraries;
it is not an OS process-kill or native UI test. The first lifecycle file is frozen.
"""

import asyncio
from types import SimpleNamespace

import pytest
from doc5_helpers import node
from graph_helpers7 import change, spec
from test_g_critic_lease_lifecycle import (
    REPORT,
    SlowCritic,
    _critic,
    _verification_done,
)

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import AttemptStatus, MissionStatus, TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.storage.store import InjectedCrash


class AfterRecordedCreation(Exception):
    """The real agent identity is committed; submit has not been called."""


async def _result(orch, intent):
    async def wait():
        while True:
            result = await orch.bridge_for(intent).result(
                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
            )
            if result is not None:
                return result
            await asyncio.sleep(0.001)

    return await asyncio.wait_for(wait(), 15)


async def _worker_candidate(orch, *, spare=False):
    mission = await orch.submit_mission(
        spec(domain=DOC_DOMAIN, success_criteria=("file:report.md",))
    )
    planning = orch.commit.begin_planning(mission.id)
    nodes = [
        node("A", tokens=60_000, outputs=["report.md"], success_criteria=["file:report.md"])
    ]
    if spare:
        nodes.append(node("B"))
    tasks, _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json({"tasks": nodes}),
        base_version=planning.version,
        source={"planner": "dispatch-gap-oracle"},
    )
    task = tasks[0]
    assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
    [attempt] = orch.store.list_attempts(task.id)
    worker = orch.store.get_intent_for_subject(attempt.id)
    assert await orch._dispatch(worker)
    worker = orch.store.get_intent(worker.intent_id)
    await orch._collect_attempt(worker, await _result(orch, worker))
    stored = orch.store.find_result_for_attempt(attempt.id)
    assert stored is not None, orch.progress_log[-12:]
    return SimpleNamespace(
        mission=mission, task=task, tasks=tasks, attempt=attempt, stored=stored,
        control=MissionControlV1(
            orch, tenant_id=mission.tenant_id, principal=Principal("public-cancel-oracle")
        ),
    )


def _reservation(orch, subject):
    with orch.store.transaction():
        return orch.commit.ledger.reservation(subject)


@pytest.mark.parametrize(
    "gap,stop",
    [("pre_submit", "task"), ("lost_submit", "task"), ("lost_submit", "mission")],
)
def test_stopped_created_critic_cold_recovery_never_submits_or_loses_real_cost(
    tmp_path, monkeypatch, gap, stop
):
    async def run():
        provider = SlowCritic()
        config = OrchestratorConfig(evidence_root=tmp_path)
        async with Orchestrator(config, provider, owner="before-reopen") as first:
            s = await _worker_candidate(first, spare=stop == "task")
            record = first.commit.record_agent_created

            def recorded_then_interrupt(intent_id, **kwargs):
                result = record(intent_id, **kwargs)
                if result.kind == "critic":
                    raise AfterRecordedCreation
                return result

            with monkeypatch.context() as patch:
                if gap == "pre_submit":
                    patch.setattr(first.commit, "record_agent_created", recorded_then_interrupt)
                    fault = AfterRecordedCreation
                else:
                    first.arm_fault("after_submit", kind="critic")
                    fault = InjectedCrash
                with pytest.raises(fault):
                    await asyncio.wait_for(first._verify(s.stored.envelope.id), 15)
            frozen = _critic(first)
            assert frozen.state == "AGENT_CREATED"
            assert frozen.agent_id and frozen.expected_turn_id
            assert frozen.receipt is None
            try:
                if gap == "lost_submit":
                    await asyncio.wait_for(provider.entered.wait(), 15)
                    assert not provider.returned.is_set()
                    live = await first.bridge_for(frozen).liveness(
                        agent_id=frozen.agent_id, turn_id=frozen.expected_turn_id
                    )
                    assert live.alive and not live.settled
                else:
                    assert provider.by_role == {"worker": 2}
                    live = await first.bridge_for(frozen).liveness(
                        agent_id=frozen.agent_id, turn_id=frozen.expected_turn_id
                    )
                    assert not live.exists

                if stop == "mission":
                    assert s.control.cancel(s.mission.id)["changed"]
                else:
                    # A cancelled Task under an ACTIVE Mission must use the same
                    # stop collector. A paused sibling keeps the Mission incomplete.
                    first.commit.commit_graph_change(
                        s.mission.id,
                        change(1, [
                            {"op": "cancel_task", "task_id": s.task.id, "reason": "route ended"},
                            {
                                "op": "pause_task", "task_id": s.tasks[1].id,
                                "reason": "hold sibling",
                            },
                        ]),
                        source={"manager": "stop-boundary-oracle"},
                    )
                    assert first.store.get_mission(s.mission.id).status is MissionStatus.ACTIVE
                    assert first.store.get_task(s.task.id).status is TaskStatus.CANCELLED
                stopped_attempt = first.store.get_attempt(s.attempt.id)
                assert stopped_attempt.status is AttemptStatus.CANCELLED
                # An AGENT_CREATED row is not proof that submit never happened.
                # The exact SDK turn must be inspected before releasing its budget.
                assert _critic(first).state == "AGENT_CREATED"
                assert _reservation(first, frozen.subject_id)["state"] != "SETTLED"
                heartbeats = first.store.count_events(s.mission.id, "HeartbeatReceived")
                reserved = first.store.count_events(s.mission.id, "BudgetReserved")
            finally:
                provider.release.set()
                if gap == "lost_submit":
                    # Let the real SDK persist its late response, while leaving
                    # Host's submit receipt absent for the next runtime to discover.
                    await _result(first, frozen)
            if gap == "lost_submit":
                assert provider.observed == [REPORT]
            expected_calls = dict(provider.by_role)

        async with Orchestrator(config, provider, owner="after-reopen") as resumed:
            submit_calls = []
            original_submit = resumed.bridge.submit

            async def submit_spy(**kwargs):
                submit_calls.append(kwargs)
                return await original_submit(**kwargs)

            def forbidden_rebind(*args, **kwargs):
                pytest.fail("stopped Critic was rebound during cold recovery")

            with monkeypatch.context() as patch:
                patch.setattr(resumed.bridge, "submit", submit_spy)
                patch.setattr(resumed, "_bind_critic", forbidden_rebind)
                assert resumed._has_inflight()
                await asyncio.wait_for(resumed.recover(), 15)
                # Across the two gaps, exercise both the direct stale dispatch
                # entry and the cycle's still-AGENT_CREATED collection branch.
                if gap == "pre_submit":
                    await resumed._dispatch(resumed.store.get_intent(frozen.intent_id))
                for _ in range(3):
                    await resumed._cycle()
            actual = _critic(resumed)
            assert actual.state == "FAILED"
            assert actual.intent_id == frozen.intent_id
            assert actual.agent_id == frozen.agent_id
            assert actual.expected_turn_id == frozen.expected_turn_id
            assert actual.config == frozen.config
            assert submit_calls == [] and provider.by_role == expected_calls
            assert resumed.store.count_events(s.mission.id, "BudgetReserved") == reserved
            assert resumed.store.count_events(s.mission.id, "HeartbeatReceived") == heartbeats
            assert resumed.store.get_attempt(s.attempt.id) == stopped_attempt
            assert resumed.store.get_task(s.task.id).accepted_result_id is None
            assert resumed.store.get_result(s.stored.envelope.id).verdict != "PASS"
            assert _reservation(resumed, actual.subject_id)["state"] == "SETTLED"
            facts = resumed.bridge_for(actual).usage_facts(agent_id=actual.agent_id)
            assert len(facts) == (2 if gap == "lost_submit" else 0)
            with resumed.store.transaction():
                tokens = resumed.commit.ledger.usage_for(actual.subject_id)[0]
                assert tokens == sum(f.input_tokens + f.output_tokens for f in facts)
                assert not resumed.commit.ledger.has_unknown_usage(actual.subject_id)
            if gap == "lost_submit":
                assert tokens > 0
                assert any(
                    r["agent_id"] == actual.agent_id and r["turn_id"] == actual.expected_turn_id
                    for r in resumed.cancel_receipts
                )
            assert not resumed._has_inflight()

    asyncio.run(run())


def test_unanswered_critic_timeout_keeps_one_intent_until_real_charge_is_collected(
    tmp_path, monkeypatch
):
    async def run():
        provider = SlowCritic()
        config = OrchestratorConfig(evidence_root=tmp_path, turn_deadline_seconds=900)
        async with Orchestrator(
            config, provider, critic_wait_seconds=2, poll_interval=0.005
        ) as orch:
            s = await _worker_candidate(orch)
            clock = SimpleNamespace(now=orch.store.now)
            monkeypatch.setattr(orch.store, "_clock", lambda: clock.now)
            verifying = asyncio.create_task(orch._verify(s.stored.envelope.id))
            try:
                await asyncio.wait_for(provider.entered.wait(), 15)
                original = _critic(orch)
                assert original.state == "SUBMITTED"
                assert not provider.returned.is_set()
                reserved = orch.store.count_events(s.mission.id, "BudgetReserved")
                clock.now += 2.01
                await _verification_done(verifying)
                assert not provider.returned.is_set()
                assert _critic(orch).intent_id == original.intent_id
                assert _critic(orch).state == "SUBMITTED"
                assert _reservation(orch, original.subject_id)["state"] != "SETTLED"
                assert orch.store.get_attempt(s.attempt.id).status is AttemptStatus.RETRY_WAIT
                assert orch.store.get_mission(s.mission.id).status is MissionStatus.ACTIVE
                assert orch._critic_subject_stopped(original) and orch._has_inflight()
                assert any(
                    r["agent_id"] == original.agent_id and r["turn_id"] == original.expected_turn_id
                    for r in orch.cancel_receipts
                )
                assert orch.store.count_events(s.mission.id, "BudgetReserved") == reserved
                # Only the stopped-subject collector runs here: do not schedule a
                # new Worker attempt while testing this original Critic's charge.
                assert not await orch._collect_stopped_critic(_critic(orch))
                assert _reservation(orch, original.subject_id)["state"] != "SETTLED"
                provider.release.set()
                await _result(orch, original)
                assert await orch._collect_stopped_critic(_critic(orch))
                assert _critic(orch).state == "FAILED"
                assert _reservation(orch, original.subject_id)["state"] == "SETTLED"
                facts = orch.bridge_for(original).usage_facts(agent_id=original.agent_id)
                assert len(facts) == 2 and all(not f.unknown for f in facts)
                assert _reservation(orch, original.subject_id)["settled_tokens"] == 300
                with orch.store.transaction():
                    assert orch.commit.ledger.usage_for(original.subject_id)[0] == sum(
                        f.input_tokens + f.output_tokens for f in facts
                    ) > 0
                assert provider.observed == [REPORT]
                assert provider.by_role == {"worker": 2, "critic": 2}
                assert orch.store.count_events(s.mission.id, "BudgetReserved") == reserved
                assert orch.store.get_task(s.task.id).accepted_result_id is None
                assert orch.store.get_result(s.stored.envelope.id).verdict != "PASS"
            finally:
                s.control.cancel(s.mission.id)
                provider.release.set()
                await _verification_done(verifying)
                intent = _critic(orch)
                if intent.state == "SUBMITTED":
                    await _result(orch, intent)
                    await orch._collect_stopped_critic(intent)

    asyncio.run(run())


def test_critic_wait_inherits_sdk_deadline_and_rejects_invalid_explicit_windows(tmp_path):
    config = OrchestratorConfig(evidence_root=tmp_path, turn_deadline_seconds=321)
    assert Orchestrator(config, SlowCritic())._critic_wait == 321
    assert Orchestrator(config, SlowCritic(), critic_wait_seconds=12)._critic_wait == 12
    assert Orchestrator(config, SlowCritic(), critic_wait_seconds=321)._critic_wait == 321
    for invalid in (0, -1, 322, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            Orchestrator(config, SlowCritic(), critic_wait_seconds=invalid)

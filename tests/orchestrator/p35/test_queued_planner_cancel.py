# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Public Mission creation, actual Planner/Worker SDK calls, one two-slot pool.

Only the external Provider is controlled. No graph, usage or receipt is seeded.
The no-estimator case exercises the Host fixture's legacy physical queue; the
estimator case retains the existing budget/UNKNOWN accounting boundary.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter, grants

from agent_orchestrator.contracts import AttemptStatus, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import (
    RoleScriptedProvider,
    UnknownAfterHandoff,
    envelope_step,
    graph_proposal_step,
    package_of,
    role_of,
)
from simple_harness.contracts import RunId


class PlannerLoad:
    def __init__(self, *, unknown_peer=False, gate_role="worker", gate_labels=None):
        self.release = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.trace = []
        self.unknown_peer = unknown_peer
        self.gate_role = gate_role
        self.gate_labels = gate_labels
        self.scripts = {}
        for label in ("A", "B", "LONG"):
            content = f"# {label}\nActual worker artifact.\n"

            def assess(request, expected=content):
                reads = [json.loads(m.content) for m in request.messages if str(m.role) == "tool"]
                met = bool(reads) and reads[-1]["value"]["content"] == expected
                return "<critic_verdict>" + json.dumps({
                    "verdict": "PASS" if met else "FAIL",
                    "findings": [] if met else [{"severity": "blocker", "detail": "bad file"}],
                    "mission_criteria": [
                        {"criterion": c, "met": met, "reason": "actual artifact equality"}
                        for c in package_of(request)["mission_success_criteria"]
                    ],
                }) + "</critic_verdict>"

            self.scripts[label] = RoleScriptedProvider({
                "planner": [graph_proposal_step([node(
                    "A", tokens=80_000,
                    verification_policy=["format_check", "rule_check", "critic_review"],
                )])],
                "worker": [
                    ("workspace_write_file", {"path": "a.md", "content": content}),
                    envelope_step(summary=label, artifacts=["a.md"], claims=[content]),
                ],
                "critic": [("workspace_read_file", {"path": "a.md"}), assess] * 2,
            })

    async def invoke(self, request, *, cancel):
        role = role_of(request)
        package = package_of(request)
        label = package["mission"]["goal"] if role == "planner" else package["mission_root_goal"]
        self.trace.append((label, role, request.request_id.value))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if role == self.gate_role and (
                self.gate_labels is None or label in self.gate_labels
            ):
                await self.release.wait()
            if role == "worker" and self.unknown_peer and label == "A":
                raise UnknownAfterHandoff("Actual Provider accepted request; response lost.")
            # This Provider can return an accepted response even after the token
            # fires. Its actual SDK usage must still settle once, without a retry.
            return await self.scripts[label].invoke(request, cancel=cancel)
        finally:
            self.active -= 1


async def until(predicate, runner):
    async def wait():
        while not predicate():
            if runner.done():
                await runner
                raise AssertionError("Orchestrator became idle before the required phase")
            await asyncio.sleep(0.002)
    await asyncio.wait_for(wait(), 15)


def account(orch, mission_id):
    with orch.store.transaction():
        report = orch.commit.ledger.costs_report(mission_id)
    return next(row for row in report["accounts"] if row["scope"] == "mission")


def invocations(orch, intent):
    return orch.bridge_for(intent).runtime.uow.list_provider_invocations(RunId(intent.agent_id))


@pytest.mark.parametrize(
    "guarded,stop_peer,unknown_peer",
    [(False, False, False), (True, False, False),
     (False, True, False), (True, True, False), (True, True, True)],
    ids=["local-queue", "budget-queue", "local-accepted", "budget-accepted", "budget-unknown"],
)
def test_cancelled_queued_planner_never_hands_off(
    tmp_path, guarded, stop_peer, unknown_peer,
):
    async def exercise():
        provider = PlannerLoad(unknown_peer=unknown_peer)
        config = OrchestratorConfig(
            evidence_root=tmp_path, max_concurrency=3, max_concurrent_model_calls=2,
            candidates_per_task=1, dynamic_graph=False,
        )
        async with Orchestrator(
            config, provider, provider_token_estimator=Counter(1000) if guarded else None,
            poll_interval=0.002,
        ) as orch:
            async def add(label):
                return await orch.submit_mission(spec(
                    label, goal=label, success_criteria=("file:a.md",),
                ))

            peers = [await add(label) for label in ("A", "B")]
            runner = asyncio.create_task(orch.run())
            try:
                await until(lambda: provider.active == 2 and {
                    label for label, role, _ in provider.trace if role == "worker"
                } == {"A", "B"}, runner)
                assert provider.peak == 2
                assert len(orch.assembled.pools) == 1  # the exact same physical queue
                queued_mission = await add("LONG")

                def queued_intent():
                    return next((i for i in orch.store.list_intents("SUBMITTED")
                                 if i.mission_id == queued_mission.id and i.kind == "plan"
                                 and invocations(orch, i)), None)

                await until(queued_intent, runner)
                queued = queued_intent()
                [before] = invocations(orch, queued)
                assert str(before.state) == "claimed" and before.handoff_attempt == 0
                assert provider.active == 2 and not any(t[0] == "LONG" for t in provider.trace)
                assert orch.store.list_tasks(queued_mission.id) == []
                [peer_intent] = [i for i in orch.store.list_intents("SUBMITTED")
                                 if i.mission_id == peers[0].id and i.kind == "attempt"]
                [accepted] = invocations(orch, peer_intent)
                assert str(accepted.state) == "handed_off" and accepted.handoff_attempt == 1

                cancelled = orch.commit.cancel_mission(queued_mission.id)
                assert cancelled.status is MissionStatus.CANCELLED
                assert orch.commit.cancel_mission(queued_mission.id) == cancelled
                if stop_peer:
                    orch.commit.cancel_mission(peers[0].id)
                # No yield permits the collector to relay cancellation first:
                # the durable Mission fence must protect newly available slots.
                provider.release.set()

                if unknown_peer:
                    await until(lambda: (
                        orch.store.get_intent(queued.intent_id).state == "FAILED"
                        and str(invocations(orch, peer_intent)[0].state) == "unknown"
                        and any(row["subject_id"] == peer_intent.subject_id
                                and row["state"] == "UNKNOWN" for row in grants(orch.commit))
                        and orch.store.get_mission(peers[1].id).status is MissionStatus.COMPLETED
                    ), runner)
                    with orch.store.transaction():
                        assert orch.commit.ledger.has_unknown_usage(peer_intent.subject_id)
                        assert (
                            orch.commit.ledger.reservation(peer_intent.subject_id)["state"]
                            == "RESERVED"
                        )
                    [unknown] = invocations(orch, peer_intent)
                    assert unknown.invocation_id == accepted.invocation_id
                    assert unknown.handoff_attempt == 1 and unknown.rehandoff_count == 0
                    assert account(orch, peers[0].id)["reserved_tokens"] > 0
                else:
                    await asyncio.wait_for(runner, 20)
                    # Repeat the actual orchestration drain: imported usage is
                    # idempotent, including an accepted call on a cancelled peer.
                    before_accounts = [account(orch, m.id) for m in peers]
                    before_trace = list(provider.trace)
                    await asyncio.wait_for(orch.run(), 10)
                    assert [account(orch, m.id) for m in peers] == before_accounts
                    assert provider.trace == before_trace
                    for mission in peers:
                        expected = (MissionStatus.CANCELLED if stop_peer and mission == peers[0]
                                    else MissionStatus.COMPLETED)
                        assert orch.store.get_mission(mission.id).status is expected
                        costs = account(orch, mission.id)
                        calls = sum(t[0] == mission.goal for t in provider.trace)
                        assert costs["settled_tokens"] == calls * 150
                        assert costs["reserved_tokens"] == 0
                        rows = orch.store.connection.execute(
                            "SELECT usage_ref FROM imported_usage WHERE mission_id=?",
                            (mission.id,),
                        ).fetchall()
                        assert len(rows) == len({row[0] for row in rows}) == calls
                    if stop_peer:
                        [settled] = invocations(orch, peer_intent)
                        assert settled.invocation_id == accepted.invocation_id
                        assert str(settled.state) == "succeeded" and settled.handoff_attempt == 1

                [after] = invocations(orch, queued)
                assert after.invocation_id == before.invocation_id
                assert after.handoff_attempt == after.rehandoff_count == 0
                assert not orch.bridge_for(queued).usage_facts(agent_id=queued.agent_id)
                assert not any(t[0] == "LONG" for t in provider.trace)
                assert orch.store.get_mission(queued_mission.id).status is MissionStatus.CANCELLED
                assert orch.store.list_tasks(queued_mission.id) == []
                costs = account(orch, queued_mission.id)
                assert costs["settled_tokens"] == costs["reserved_tokens"] == 0
                assert provider.peak == 2 and provider.active == 0
                assert len(provider.trace) == len({t[2] for t in provider.trace})
                if stop_peer:
                    assert sum(t[0] == "A" and t[1] == "worker" for t in provider.trace) == 1
            finally:
                provider.release.set()
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "cancel_queued,stall_after_queue", [(False, False), (True, False), (False, True)],
    ids=["complete", "cancel", "real-stall-after-queue"],
)
def test_local_worker_queue_survives_stall_threshold_through_agent_bridge(
    tmp_path, monkeypatch, cancel_queued, stall_after_queue,
):
    async def exercise():
        # Planner holders are service turns, so their deliberately slow Provider
        # calls do not themselves exercise the Worker's executor_stalled rule.
        provider = PlannerLoad(gate_role="planner", gate_labels={"A", "B"})
        config = OrchestratorConfig(
            evidence_root=tmp_path, max_concurrency=3, max_concurrent_model_calls=2,
            candidates_per_task=1, dynamic_graph=False,
            stall_seconds=0.4 if stall_after_queue else 0.2,
            lease_seconds=0.5, sdk_lease_ttl_seconds=0.2,
        )
        async with Orchestrator(config, provider, poll_interval=0.002) as orch:
            runner = None
            exit_ready, resume_executor = asyncio.Event(), asyncio.Event()
            try:
                async def plan(label):
                    mission = await orch.submit_mission(spec(
                        label, goal=label, success_criteria=("file:a.md",),
                    ))
                    await orch._start_planning(mission)
                    [intent] = [i for i in orch.store.list_intents("PENDING")
                                if i.mission_id == mission.id and i.kind == "plan"]
                    assert await orch._dispatch(intent)
                    return mission, orch.store.get_intent(intent.intent_id)

                # Drive the actual Planner first, without yet allocating its
                # Worker. No graph or execution receipt is manufactured.
                queued_mission, planning = await plan("LONG")
                bridge = orch.bridge_for(planning)
                agent = await bridge.runtime.open(planning.agent_id)
                await agent.wait_turn(planning.expected_turn_id, timeout=10)
                assert await orch._collect(planning)
                [task] = orch.store.list_tasks(queued_mission.id)
                assert task.status is TaskStatus.READY
                assert orch.store.list_attempts(task.id) == []

                if stall_after_queue:
                    local_admission = bridge.runtime.effective_provider_admission
                    acquire = local_admission.acquire

                    async def pause_after_real_acquire(**kwargs):
                        ticket = await acquire(**kwargs)
                        request = kwargs["request"]
                        if (role_of(request) == "worker"
                                and package_of(request)["mission_root_goal"] == "LONG"):
                            # Pause the actual executor after it leaves the actual
                            # queue. Do not replace liveness or seed SDK progress.
                            exit_ready.set()
                            await resume_executor.wait()
                        return ticket

                    monkeypatch.setattr(local_admission, "acquire", pause_after_real_acquire)

                peers = [(await plan(label))[0] for label in ("A", "B")]

                async def occupied():
                    while provider.active != 2:
                        await asyncio.sleep(0.002)

                await asyncio.wait_for(occupied(), 10)
                assert len(orch.assembled.pools) == 1
                assert provider.peak == 2
                runner = asyncio.create_task(orch.run())

                def queued_intent():
                    return next((i for i in orch.store.list_intents("SUBMITTED")
                                 if i.mission_id == queued_mission.id and i.kind == "attempt"
                                 and invocations(orch, i)), None)

                await until(queued_intent, runner)
                queued = queued_intent()
                bridge = orch.bridge_for(queued)
                runtime = bridge.runtime
                # Effective admission is observable, but its local slot tracking
                # must not opt legacy consumers into budget-grant accounting.
                assert runtime.ports.provider_admission is None
                admission = runtime.effective_provider_admission
                assert admission is not None
                [original] = invocations(orch, queued)
                assert original.handoff_attempt == 0
                baseline = await bridge.liveness(
                    agent_id=queued.agent_id, turn_id=queued.expected_turn_id,
                )
                assert baseline.alive and baseline.state == "running"
                assert baseline.blocked
                assert baseline.blocker == {"kind": "provider_slot_wait", "billable": False}
                trace = list(provider.trace)
                start = asyncio.get_running_loop().time()
                # Six stall windows, and multiple real lease-renewal cycles.
                # Actual SDK progress must remain unchanged throughout the wait.
                while asyncio.get_running_loop().time() - start < 6 * config.stall_seconds:
                    await asyncio.sleep(0.01)
                    if runner.done():
                        await runner
                        raise AssertionError(
                            "queue drain stopped while Provider slots were occupied"
                        )
                    live = await bridge.liveness(
                        agent_id=queued.agent_id, turn_id=queued.expected_turn_id,
                    )
                    assert live.alive and live.blocked and live.blocker == baseline.blocker
                    assert live.progress == baseline.progress
                    assert admission.waiting_for_slot(
                        agent_id=queued.agent_id, turn_id=queued.expected_turn_id,
                    )
                    current = orch.store.get_attempt(queued.subject_id)
                    assert current.status is AttemptStatus.RUNNING
                    assert len(orch.store.list_attempts(task.id)) == 1
                    assert provider.trace == trace and provider.active == 2
                    [record] = invocations(orch, queued)
                    assert record.invocation_id == original.invocation_id
                    assert record.handoff_attempt == 0
                    assert not bridge.usage_facts(agent_id=queued.agent_id)

                current = orch.store.get_attempt(queued.subject_id)
                assert current.progress_at is not None
                assert orch.store.now - current.progress_at > config.stall_seconds
                assert not {"AttemptTimedOut", "AttemptLost"} & {
                    event.type for event in orch.store.list_events(queued_mission.id)
                }
                if cancel_queued:
                    cancelled = orch.commit.cancel_mission(queued_mission.id)
                    assert cancelled.status is MissionStatus.CANCELLED
                provider.release.set()
                if stall_after_queue:
                    await asyncio.wait_for(exit_ready.wait(), 10)
                    previous_progress_at = current.progress_at
                    await until(
                        lambda: orch.store.get_attempt(queued.subject_id).progress_at
                        > previous_progress_at,
                        runner,
                    )
                    resumed = orch.store.get_attempt(queued.subject_id)
                    assert resumed.status is AttemptStatus.RUNNING
                    assert resumed.progress_marker == current.progress_marker
                    live = await bridge.liveness(
                        agent_id=queued.agent_id, turn_id=queued.expected_turn_id,
                    )
                    assert live.alive and not live.blocked and live.progress == baseline.progress
                    await until(
                        lambda: orch.store.get_attempt(queued.subject_id).status
                        is AttemptStatus.TIMED_OUT,
                        runner,
                    )
                    timed_out = orch.store.get_attempt(queued.subject_id)
                    assert timed_out.failure["reason"] == "executor_stalled"
                    assert timed_out.progress_at == resumed.progress_at
                    assert timed_out.progress_marker == resumed.progress_marker
                    events = [e for e in orch.store.list_events(queued_mission.id)
                              if e.attempt_id == queued.subject_id]
                    [timeout] = [e for e in events if e.type == "AttemptTimedOut"]
                    assert timeout.created_at - resumed.progress_at >= config.stall_seconds
                    # A normal lease renewal happened inside the new window;
                    # unchanged progress did not buy another stall allowance.
                    assert any(
                        e.type == "HeartbeatReceived"
                        and resumed.progress_at + config.lease_seconds / 2 <= e.created_at
                        < timeout.created_at
                        for e in events
                    )
                    [record] = invocations(orch, queued)
                    assert record.handoff_attempt == 0
                    orch.commit.cancel_mission(queued_mission.id)
                    resume_executor.set()
                    await asyncio.wait_for(runner, 20)
                    return
                await asyncio.wait_for(runner, 20)
                assert not admission.waiting_for_slot(
                    agent_id=queued.agent_id, turn_id=queued.expected_turn_id,
                )
                final_live = await bridge.liveness(
                    agent_id=queued.agent_id, turn_id=queued.expected_turn_id,
                )
                assert not final_live.alive and not final_live.blocked
                assert provider.peak == 2 and provider.active == 0
                assert len(orch.store.list_attempts(task.id)) == 1
                assert not {"AttemptTimedOut", "AttemptLost"} & {
                    event.type for event in orch.store.list_events(queued_mission.id)
                }
                for peer in peers:
                    assert orch.store.get_mission(peer.id).status is MissionStatus.COMPLETED
                assert account(orch, queued_mission.id)["reserved_tokens"] == 0
                if cancel_queued:
                    final_mission = orch.store.get_mission(queued_mission.id)
                    assert final_mission.status is MissionStatus.CANCELLED
                    [record] = invocations(orch, queued)
                    assert record.handoff_attempt == record.rehandoff_count == 0
                    assert not any(label == "LONG" and role == "worker"
                                   for label, role, _ in provider.trace)
                    assert account(orch, queued_mission.id)["settled_tokens"] == 150  # real Planner
                else:
                    final_mission = orch.store.get_mission(queued_mission.id)
                    assert final_mission.status is MissionStatus.COMPLETED
                    [record] = [r for r in invocations(orch, queued)
                                if r.invocation_id == original.invocation_id]
                    assert record.handoff_attempt == 1 and record.rehandoff_count == 0
            finally:
                resume_executor.set()
                provider.release.set()
                if runner is not None:
                    if not runner.done():
                        runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(exercise())

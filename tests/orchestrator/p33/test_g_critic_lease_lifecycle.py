# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Real SDK Critic polling, lease authority and public Mission cancellation.

The Event blocks the Provider after a real workspace read, before its verdict.
Only the orchestration clock is controlled; SDK execution/usage remains real.
No verification rows, PASS receipts, leases or terminal states are fabricated.
These are deterministic runtime controls, not model-quality or native UI evidence.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from doc5_helpers import node
from fixtures_provider import RoleScriptedProvider, envelope_step, package_of, role_of
from graph_helpers7 import spec

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import AttemptStatus, MissionStatus, TaskStatus
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.commit_service import CommitRejected
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig

REPORT = "# 报告\n风险：材料未覆盖离线运行。\n"
LEASE = 60.0
INTENT_STATES = ("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED", "SETTLED", "FAILED")


class SlowCritic(RoleScriptedProvider):
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()
        self.observed = []
        super().__init__(
            {
                "worker": [
                    ("workspace_write_file", {"path": "report.md", "content": REPORT}),
                    envelope_step(
                        summary="报告已写入",
                        artifacts=["report.md"],
                        claims=["报告已写入 report.md。"],
                    ),
                ],
                "critic": [("workspace_read_file", {"path": "report.md"}), self.judge],
            }
        )

    def judge(self, request):
        reads = [
            json.loads(message.content)
            for message in request.messages
            if str(message.role) == "tool"
        ]
        content = reads[-1]["value"]["content"]
        self.observed.append(content)
        meets = content == REPORT and "风险：" in content
        verdict = {
            "verdict": "PASS" if meets else "FAIL",
            "findings": [] if meets else [{"severity": "blocker", "detail": "实际报告不完整"}],
            "mission_criteria": [
                {"criterion": c, "met": meets, "reason": "已读取实际报告文件并检查风险段落"}
                for c in package_of(request)["mission_success_criteria"]
            ],
        }
        return "<critic_verdict>" + json.dumps(verdict, ensure_ascii=False) + "</critic_verdict>"

    async def invoke(self, request, *, cancel):
        held = role_of(request) == "critic" and any(
            str(message.role) == "tool" for message in request.messages
        )
        if held:
            self.entered.set()
            # Deliberately finish the actual in-flight response after cooperative
            # cancellation: its known cost must still reach the durable ledger.
            await self.release.wait()
        response = await super().invoke(request, cancel=cancel)
        if held:
            self.returned.set()
        return response


async def _until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 15)


async def _verification_done(task):
    outcome = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15))[0]
    if isinstance(outcome, BaseException) and not isinstance(outcome, CommitRejected):
        raise outcome
    return outcome


def _critic(orch):
    [intent] = [i for i in orch.store.list_intents(*INTENT_STATES) if i.kind == "critic"]
    return intent


def _heartbeats(s):
    return s.orch.store.count_events(s.mission.id, "HeartbeatReceived")


async def _drain_stopped(s):
    async def collect():
        while _critic(s.orch).state != "FAILED":
            await s.orch._cycle()
            await asyncio.sleep(0.001)

    await asyncio.wait_for(collect(), 15)


@asynccontextmanager
async def _slow_run(tmp_path, monkeypatch):
    provider = SlowCritic()
    async with Orchestrator(
        OrchestratorConfig(evidence_root=tmp_path, lease_seconds=LEASE),
        provider,
        owner="lease-owner",
        poll_interval=0.005,
    ) as orch:
        mission = await orch.submit_mission(
            spec(domain=DOC_DOMAIN, success_criteria=("file:report.md",))
        )
        planning = orch.commit.begin_planning(mission.id)
        [task], _ = orch.commit.commit_task_graph(
            mission.id,
            TaskGraphProposal.from_json(
                {
                    "tasks": [
                        node(
                            "A",
                            tokens=60_000,
                            outputs=["report.md"],
                            success_criteria=["file:report.md"],
                        )
                    ]
                }
            ),
            base_version=planning.version,
            source={"planner": "critic-lease-oracle"},
        )
        assert await orch._next_attempt(orch.store.get_mission(mission.id), task, [])
        [attempt] = orch.store.list_attempts(task.id)
        worker = orch.store.get_intent_for_subject(attempt.id)
        assert await orch._dispatch(worker)
        worker = orch.store.get_intent(worker.intent_id)

        async def completed():
            while True:
                result = await orch.bridge_for(worker).result(
                    agent_id=worker.agent_id, turn_id=worker.expected_turn_id
                )
                if result is not None:
                    return result
                await asyncio.sleep(0.001)

        result = await asyncio.wait_for(completed(), 15)
        await orch._collect_attempt(worker, result)
        stored = orch.store.find_result_for_attempt(attempt.id)
        assert stored is not None, orch.progress_log[-12:]
        clock = SimpleNamespace(now=orch.store.now)
        monkeypatch.setattr(orch.store, "_clock", lambda: clock.now)
        ticks = SimpleNamespace(count=0)
        original_hold = orch._hold_lease

        def hold(attempt_id):
            renewed = original_hold(attempt_id)
            ticks.count += 1
            return renewed

        monkeypatch.setattr(orch, "_hold_lease", hold)
        verifying = asyncio.create_task(orch._verify(stored.envelope.id))
        control = MissionControlV1(
            orch, tenant_id=mission.tenant_id, principal=Principal("ui-equivalent-owner")
        )
        s = SimpleNamespace(
            orch=orch, mission=mission, task=task, attempt=attempt, stored=stored,
            provider=provider, verifying=verifying, clock=clock, ticks=ticks, control=control,
        )
        try:
            await asyncio.wait_for(provider.entered.wait(), 15)
            assert _critic(orch).state == "SUBMITTED"
            assert orch.store.get_attempt(attempt.id).status is AttemptStatus.VERIFYING
            yield s
        finally:
            # Release every real SDK task even when an assertion fails. Cleanup
            # is not part of any oracle's claimed evidence.
            control.cancel(mission.id)
            provider.release.set()
            await _verification_done(verifying)
            if any(i.kind == "critic" for i in orch.store.list_intents(*INTENT_STATES)):
                if _critic(orch).state == "SUBMITTED":
                    await _drain_stopped(s)


def test_slow_critic_polls_without_writes_until_half_lease_then_really_accepts(
    tmp_path, monkeypatch
):
    async def run():
        async with _slow_run(tmp_path, monkeypatch) as s:
            orch = s.orch
            before = orch._hold_lease(s.attempt.id)
            events = _heartbeats(s)
            ticks = s.ticks.count
            await _until(lambda: s.ticks.count >= ticks + 8)
            assert not s.verifying.done() and not s.provider.release.is_set()
            assert orch.store.get_attempt(before.id) == before
            assert _heartbeats(s) == events

            # At exactly half remaining, renewal is due (strict > fast return).
            s.clock.now = before.lease_expires_at - LEASE / 2
            await _until(lambda: orch.store.get_attempt(before.id).version > before.version)
            renewed = orch.store.get_attempt(before.id)
            assert renewed.version == before.version + 1
            assert renewed.lease_expires_at == s.clock.now + LEASE
            assert _heartbeats(s) == events + 1
            ticks = s.ticks.count
            await _until(lambda: s.ticks.count >= ticks + 8)
            assert orch.store.get_attempt(before.id) == renewed
            assert _heartbeats(s) == events + 1

            s.provider.release.set()
            assert await _verification_done(s.verifying) is True
            assert s.provider.observed == [REPORT]
            assert orch.store.get_result(s.stored.envelope.id).verdict == "PASS"
            assert orch.store.get_task(s.task.id).accepted_result_id == s.stored.envelope.id
            critic = _critic(orch)
            assert critic.state == "SETTLED"
            assert orch.store.get_receipt("critic-verdict:" + critic.intent_id) is not None

    asyncio.run(run())


def test_renew_lease_throttle_is_opt_in_and_real_progress_still_writes(tmp_path, monkeypatch):
    async def run():
        async with _slow_run(tmp_path, monkeypatch) as s:
            orch = s.orch
            before = orch._hold_lease(s.attempt.id)
            args = dict(
                owner="lease-owner", lease_seconds=LEASE,
                liveness={"progress": before.progress_marker, "phase": "verifying"},
            )
            events = orch.store.list_events(s.mission.id)
            for _ in range(8):
                assert orch.commit.renew_lease(
                    before.id, **args, minimum_remaining_seconds=LEASE / 2
                ) == before
            assert orch.store.list_events(s.mission.id) == events
            # Legacy Worker callers omit the new option and still renew each call.
            s.clock.now += 0.01
            default = orch.commit.renew_lease(before.id, **args)
            assert default.version == before.version + 1
            s.clock.now += 0.01
            explicit_zero = orch.commit.renew_lease(
                before.id, **args, minimum_remaining_seconds=0.0
            )
            assert explicit_zero.version == default.version + 1
            s.clock.now += 0.01
            progress = (before.progress_marker or 0) + 1
            changed = orch.commit.renew_lease(
                before.id, owner="lease-owner", lease_seconds=LEASE,
                liveness={"progress": progress, "phase": "verifying"},
                minimum_remaining_seconds=LEASE / 2,
            )
            assert changed.version == explicit_zero.version + 1
            assert changed.progress_marker == progress and changed.progress_at == s.clock.now
            assert _heartbeats(s) == len([e for e in events if e.type == "HeartbeatReceived"]) + 3

    asyncio.run(run())


def test_new_live_owner_rejects_old_critic_without_cancelling_its_turn(tmp_path, monkeypatch):
    async def run():
        async with _slow_run(tmp_path, monkeypatch) as s:
            orch = s.orch
            before = orch.store.get_attempt(s.attempt.id)
            s.clock.now = before.lease_expires_at + 0.01
            current = orch.commit.renew_lease(
                before.id, owner="new-owner", lease_seconds=LEASE,
                liveness={"progress": before.progress_marker},
                minimum_remaining_seconds=LEASE / 2,
            )
            events = orch.store.list_events(s.mission.id)
            for _ in range(3):
                with pytest.raises(CommitRejected):
                    orch._hold_lease(before.id)
                with pytest.raises(CommitRejected):
                    orch.commit.renew_lease(
                        before.id, owner="lease-owner", lease_seconds=LEASE,
                        liveness={"progress": before.progress_marker},
                        minimum_remaining_seconds=LEASE / 2,
                    )
            await _verification_done(s.verifying)
            assert orch.store.get_attempt(before.id) == current
            assert orch.store.list_events(s.mission.id) == events
            assert not orch._critic_subject_stopped(_critic(orch))
            assert _critic(orch).state == "SUBMITTED"
            assert orch.cancel_receipts == []
            assert not s.provider.returned.is_set()
            assert orch.store.get_task(s.task.id).accepted_result_id is None

    asyncio.run(run())


def test_public_cancel_stops_renewal_but_settles_actual_late_critic_usage(tmp_path, monkeypatch):
    async def run():
        async with _slow_run(tmp_path, monkeypatch) as s:
            orch = s.orch
            cancelled = s.control.cancel(s.mission.id)
            assert cancelled["changed"] and cancelled["status"] == "CANCELLED"
            after = orch.store.get_attempt(s.attempt.id)
            assert after.status is AttemptStatus.CANCELLED
            events = orch.store.list_events(s.mission.id)
            heartbeats = _heartbeats(s)
            for minimum in (0.0, LEASE / 2):
                for _ in range(3):
                    with pytest.raises(CommitRejected):
                        orch._hold_lease(after.id)
                    with pytest.raises(CommitRejected):
                        orch.commit.renew_lease(
                            after.id, owner="lease-owner", lease_seconds=LEASE,
                            liveness={"progress": after.progress_marker},
                            minimum_remaining_seconds=minimum,
                        )
            assert orch.store.get_attempt(after.id) == after
            assert orch.store.list_events(s.mission.id) == events
            await _verification_done(s.verifying)
            critic = _critic(orch)
            assert critic.state == "SUBMITTED" and orch._has_inflight()
            receipts = [r for r in orch.cancel_receipts if r["attempt_id"] == critic.subject_id]
            assert len(receipts) == 1
            assert receipts[0]["agent_id"] == critic.agent_id
            assert receipts[0]["turn_id"] == critic.expected_turn_id
            assert receipts[0]["command_id"] == critic.subject_id + ":cancel"
            # Repeated real cycles cannot issue another cancel or heartbeat.
            for _ in range(3):
                await orch._cycle()
            assert orch.cancel_receipts == receipts
            assert orch.store.get_attempt(after.id) == after

            s.provider.release.set()
            await asyncio.wait_for(s.provider.returned.wait(), 15)
            await _drain_stopped(s)
            assert s.provider.observed == [REPORT]
            assert _critic(orch).state == "FAILED"
            facts = orch.bridge_for(critic).usage_facts(agent_id=critic.agent_id)
            assert len(facts) == 2 and all(not fact.unknown for fact in facts)
            expected_tokens = sum(f.input_tokens + f.output_tokens for f in facts)
            with orch.store.transaction():
                assert orch.commit.ledger.usage_for(critic.subject_id)[0] == expected_tokens > 0
                assert not orch.commit.ledger.has_unknown_usage(critic.subject_id)
                reservation = orch.commit.ledger.reservation(critic.subject_id)
            assert reservation["state"] == "SETTLED"
            assert orch.store.get_mission(s.mission.id).status is MissionStatus.CANCELLED
            assert orch.store.get_attempt(after.id) == after
            assert _heartbeats(s) == heartbeats
            assert orch.store.get_task(s.task.id).status is TaskStatus.CANCELLED
            assert orch.store.get_task(s.task.id).accepted_result_id is None
            assert orch.store.get_result(s.stored.envelope.id).verdict != "PASS"
            assert all(
                str(claim.status) == "REJECTED"
                for claim in orch.store.list_claims(s.stored.envelope.id)
            )
            assert not orch._has_inflight()
            settled_events = orch.store.list_events(s.mission.id)
            for _ in range(3):
                await orch._cycle()
            assert orch.store.list_events(s.mission.id) == settled_events
            assert orch.cancel_receipts == receipts

    asyncio.run(run())


def test_cancel_between_preliminary_read_and_transaction_cannot_fast_return(tmp_path, monkeypatch):
    async def run():
        async with _slow_run(tmp_path, monkeypatch) as s:
            orch = s.orch
            before = orch._hold_lease(s.attempt.id)
            heartbeats = _heartbeats(s)
            original = orch.store.get_attempt
            injected = False

            def cancel_after_read(attempt_id):
                nonlocal injected
                stale = original(attempt_id)
                if not injected and not orch.store.connection.in_transaction:
                    injected = True
                    s.control.cancel(s.mission.id)
                return stale

            with monkeypatch.context() as patch:
                patch.setattr(orch.store, "get_attempt", cancel_after_read)
                with pytest.raises(CommitRejected):
                    orch._hold_lease(before.id)
            assert injected
            after = original(before.id)
            assert after.status is AttemptStatus.CANCELLED
            assert (
                after.lease_expires_at is None
                or after.lease_expires_at <= before.lease_expires_at
            )
            assert _heartbeats(s) == heartbeats

    asyncio.run(run())

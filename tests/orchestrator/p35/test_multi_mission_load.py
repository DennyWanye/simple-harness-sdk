# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""P35 §7 sidecar: three real Missions share two physical SDK provider slots.

Graphs use the public Commit fixture seam; Workers, tools, admission, verification,
Critic reads, review and cancellation run through production. No injected PASS.
A01: observed physical peak == 2. A02/A04: actual queued SDK invocation has no
handoff/usage or duplicate Attempt. A03 (partial): local verification and human
wait consume no model slot; this is not a watermark/priority-drain oracle.
A06 (partial): public cancellation fences queued handoff while peers complete.
No priced, multi-profile/process, long-context, OS-kill, backup or UI claim.
"""

from __future__ import annotations

import asyncio
import json

from fixtures_provider import RoleScriptedProvider, envelope_step, package_of
from graph_helpers7 import node, spec
from helpers_step07 import ALICE
from test_provider_budget_guard import Counter, grants

from agent_orchestrator.artifacts.store import read_verified
from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import role_of
from simple_harness.contracts import RunId


class MeasuredProvider:
    """Count at the actual provider boundary, not at logical dispatch/semaphore."""

    def __init__(self):
        self.release_workers = asyncio.Event()
        self.active = 0
        self.peak = 0
        self.trace = []
        self.reviewed = []
        self.scripts = {}
        for label in ("slow", "human", "cancel"):
            report = f"# {label}\nVerified fixture material for {label}.\n"

            def assess(request, *, expected=report, identity=label):
                reads = [
                    json.loads(message.content)
                    for message in request.messages
                    if str(message.role) == "tool"
                ]
                actual = reads[-1]["value"]["content"]
                self.reviewed.append((identity, actual))
                met = actual == expected
                return (
                    "<critic_verdict>"
                    + json.dumps(
                        {
                            "verdict": "PASS" if met else "FAIL",
                            "findings": []
                            if met
                            else [{"severity": "blocker", "detail": "bad file"}],
                            "mission_criteria": [
                                {"criterion": c, "met": met, "reason": "actual file equality"}
                                for c in package_of(request)["mission_success_criteria"]
                            ],
                        }
                    )
                    + "</critic_verdict>"
                )

            self.scripts[label] = RoleScriptedProvider(
                {
                    "worker": [
                        ("workspace_write_file", {"path": "a.md", "content": report}),
                        envelope_step(
                            summary=f"wrote {label}",
                            artifacts=["a.md"],
                            claims=[f"a.md contains {label} fixture material"],
                        ),
                    ],
                    "critic": [("workspace_read_file", {"path": "a.md"}), assess] * 2,
                }
            )

    async def invoke(self, request, *, cancel):
        label = package_of(request)["mission_root_goal"]
        role = role_of(request)
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.trace.append((label, role, str(request.request_id)))
        try:
            if role == "worker":
                await self.release_workers.wait()
            return await self.scripts[label].invoke(request, cancel=cancel)
        finally:
            self.active -= 1


async def _until(predicate, runner):
    async def wait():
        while not predicate():
            if runner.done():
                await runner  # surface the real runtime failure before a timeout
                raise AssertionError("orchestrator became idle before the required phase")
            await asyncio.sleep(0.002)

    await asyncio.wait_for(wait(), 10)


async def _mission(orch, label):
    mission = await orch.submit_mission(spec(label, goal=label, success_criteria=("file:a.md",)))
    planning = orch.commit.begin_planning(mission.id)
    policy = ["format_check", "rule_check"]
    policy.append("human_review" if label == "human" else "critic_review")
    tasks, _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {"tasks": [node("A", tokens=80_000, verification_policy=policy)]}
        ),
        base_version=planning.version,
        source={"planner": "deterministic load fixture; no model planning claim"},
    )
    return mission, tasks[0]


def _facts(orch, missions, provider):
    """Read-only sidecar; output is retained by pytest/main's ignored evidence log."""
    rows = grants(orch.commit)
    facts = {"max_inflight": provider.peak, "missions": {}}
    for label, (mission, task) in missions.items():
        own = [r for r in rows if orch.store.get_intent(r["intent_id"]).mission_id == mission.id]
        with orch.store.transaction():
            costs = orch.commit.ledger.costs_report(mission.id)
        account = next(a for a in costs["accounts"] if a["scope"] == "mission")
        facts["missions"][label] = {
            "mission_id": mission.id,
            "status": str(orch.store.get_mission(mission.id).status),
            "task_status": str(orch.store.get_task(task.id).status),
            "attempts": [a.id for a in orch.store.list_attempts(task.id)],
            "provider_calls": sum(item[0] == label for item in provider.trace),
            "grant_states": [r["state"] for r in own],
            "actual_tokens": sum(r["actual_tokens"] or 0 for r in own),
            "reserved_tokens": account["reserved_tokens"],
            "settled_tokens": account["settled_tokens"],
            "events": [e.type for e in orch.store.list_events(mission.id)],
        }
    return facts


def test_three_missions_two_slots_cancel_queue_and_nonmodel_waits(tmp_path, monkeypatch):
    async def exercise():
        provider = MeasuredProvider()
        release_verifier = asyncio.Event()
        verifier_entered = asyncio.Event()
        config = OrchestratorConfig(
            evidence_root=tmp_path / "load",
            max_concurrency=3,
            candidates_per_task=1,
            max_concurrent_model_calls=2,
            verifier_workers=2,
        )
        assert config.max_concurrent_model_calls == 2
        async with Orchestrator(
            config, provider, provider_token_estimator=Counter(1000), poll_interval=0.002
        ) as orch:
            original_verify = orch._router.verify

            async def slow_local_verify(**kwargs):
                if kwargs["mission"].goal == "slow":
                    verifier_entered.set()
                    await release_verifier.wait()
                return await original_verify(**kwargs)

            monkeypatch.setattr(orch._router, "verify", slow_local_verify)
            missions = {label: await _mission(orch, label) for label in ("slow", "human")}
            runner = asyncio.create_task(orch.run())
            try:
                await _until(lambda: provider.active == 2, runner)
                assert {item[0] for item in provider.trace} == {"slow", "human"}
                missions["cancel"] = await _mission(orch, "cancel")
                cancelled, cancelled_task = missions["cancel"]

                def queued_intent():
                    for intent in orch.store.list_intents("SUBMITTED"):
                        if intent.mission_id == cancelled.id and intent.kind == "attempt":
                            if orch._provider_admission.waiting_for_slot(
                                agent_id=intent.agent_id, turn_id=intent.expected_turn_id
                            ):
                                return intent
                    return None

                await _until(queued_intent, runner)
                queued = queued_intent()
                pool = orch.assembled.pools[orch.profile_of(queued)]
                live = await pool.bridge.liveness(
                    agent_id=queued.agent_id, turn_id=queued.expected_turn_id
                )
                assert live.alive and live.blocked
                assert live.blocker["kind"] == "provider_slot_wait"
                assert live.blocker["billable"] is False
                records = pool.runtime.uow.list_provider_invocations(RunId(queued.agent_id))
                assert len(records) == 1 and records[0].handoff_attempt == 0
                assert not pool.bridge.usage_facts(agent_id=queued.agent_id)
                attempts_before = orch.store.list_attempts(cancelled_task.id)
                assert len(attempts_before) == 1
                before = _facts(orch, missions, provider)
                # Repeated sidecar reads do not mutate state or consume a slot.
                assert _facts(orch, missions, provider) == before
                assert provider.active == 2
                orch.commit.cancel_mission(cancelled.id)
                orch.commit.cancel_mission(cancelled.id)  # public idempotency
                provider.release_workers.set()

                human_id = missions["human"][0].id

                def pending_review():
                    return [
                        r for r in orch.store.list_approvals(human_id) if r["state"] == "PENDING"
                    ]

                await _until(lambda: verifier_entered.is_set() and pending_review(), runner)
                assert provider.active == 0
                assert orch.store.get_task(missions["slow"][1].id).status is TaskStatus.VERIFYING
                [review] = pending_review()
                assert review["kind"] == "review"
                assert (
                    orch.store.get_result(review["subject_key"]).verification_state == "SUSPENDED"
                )
                waiting = _facts(orch, missions, provider)
                assert _facts(orch, missions, provider) == waiting
                release_verifier.set()
                await asyncio.wait_for(runner, 15)
                assert (
                    orch.store.get_mission(missions["slow"][0].id).status is MissionStatus.COMPLETED
                )
                assert pending_review()  # peer's real Critic progressed despite human wait
                assert any(label == "slow" for label, _ in provider.reviewed)
                human_artifacts = orch.store.list_mission_artifacts(human_id)
                [report] = [a for a in human_artifacts if a.path == "a.md"]
                assert read_verified(report).decode() == (
                    "# human\nVerified fixture material for human.\n"
                )
                orch.commit.review_result(
                    review["request_id"],
                    principal=ALICE,
                    verdict="pass",
                    note="Read the actual fixture report and approve it",
                    nonce="load-review",
                )
                await asyncio.wait_for(orch.run(), 15)
                final = _facts(orch, missions, provider)
                print(json.dumps({"queued": before, "waiting": waiting, "final": final}))
                assert provider.peak == 2 and provider.active == 0
                assert len({item[2] for item in provider.trace}) == len(provider.trace)
                for label in ("slow", "human"):
                    facts = final["missions"][label]
                    assert facts["status"] == str(MissionStatus.COMPLETED), final
                    assert len(facts["attempts"]) == 1
                    assert facts["grant_states"] and set(facts["grant_states"]) == {"SETTLED"}
                    assert facts["settled_tokens"] == facts["actual_tokens"] > 0
                    assert facts["reserved_tokens"] == 0
                    assert "AttemptLost" not in facts["events"]
                facts = final["missions"]["cancel"]
                assert facts["status"] == str(MissionStatus.CANCELLED)
                assert facts["attempts"] == [attempts_before[0].id]
                assert (
                    facts["provider_calls"]
                    == facts["actual_tokens"]
                    == facts["settled_tokens"]
                    == 0
                )
                assert facts["reserved_tokens"] == 0
                assert set(facts["grant_states"]) <= {"RELEASED"}
                assert not {"AttemptLost", "ResultAccepted", "VerificationPassed"} & set(
                    facts["events"]
                )
                records = pool.runtime.uow.list_provider_invocations(RunId(queued.agent_id))
                assert len(records) == 1 and records[0].handoff_attempt == 0
            finally:
                provider.release_workers.set()
                release_verifier.set()
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(exercise())

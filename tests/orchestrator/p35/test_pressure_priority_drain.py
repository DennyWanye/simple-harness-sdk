# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A03: one real Mission loop joins pressure, priority drain and original tails.

The graph is a public planning fixture. Every result, source read, file write,
Critic verdict, arbitration request and accounting transfer is made by the
production runtime/SDK; this test only gates physical Provider calls.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step, package_of
from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter

from agent_orchestrator.api.facade import MissionControlV1
from agent_orchestrator.contracts import MissionStatus, TaskStatus
from agent_orchestrator.governance import domains
from agent_orchestrator.governance.domains import DOC_DOMAIN
from agent_orchestrator.governance.permissions import Principal
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import role_of

LIMIT = 3
QUOTES = {"a": "资料甲适用室内环境。", "b": "资料乙适用室外环境。"}


class CompositeProvider:
    def __init__(self) -> None:
        self.versions: dict[str, str] = {}
        self.synthesis_package_ids: list[tuple[str, ...]] = []
        self.b_task_id = ""
        self.b_entered = asyncio.Event()
        self.release_b = asyncio.Event()
        self.p2_entered = asyncio.Event()
        self.release_p2 = asyncio.Event()
        self.critic_entered = {key: asyncio.Event() for key in ("P1", "P2")}
        self.release_critic = {key: asyncio.Event() for key in ("P1", "P2")}
        self.calls: list[tuple[str, str, str]] = []
        self.scripts = {"doc": self._document()}
        for label in ("P1", "P2", "fresh"):
            self.scripts[label] = self._ordinary(label)

    def cited(self, body: dict, key: str) -> dict:
        selected = "ab" if key in {"arb", "s"} else key
        citations = [
            {
                "path": f"sources/{source}.md",
                "version": self.versions[source],
                "start_line": 1,
                "end_line": 1,
                "quote": QUOTES[source],
            }
            for source in selected
        ]
        claims = [
            {
                "content": "实际离线能力需要人核查。",
                "confidence": 0.8,
                "key": "world.offline" if key != "s" else "world.summary",
                "stance": "refutes" if key == "b" else "affirms",
                "citations": citations,
            }
        ]
        if key in QUOTES:
            # The disputed world.offline claim stays disputed. This independent,
            # source-quoted claim must earn VERIFIED status through the real Worker
            # verification path before synthesis may cite it.
            claims.append(
                {
                    "content": QUOTES[key],
                    "confidence": 0.8,
                    "key": f"source.{key}",
                    "stance": "affirms",
                    "citations": citations,
                }
            )
        return {
            **body,
            "evidence": [] if key != "s" else body["evidence"],
            "claims": claims,
            "used_knowledge": [],
        }

    def _document(self) -> RoleScriptedProvider:
        def work(key: str) -> list[object]:
            return [
                ("workspace_read_file", {"path": f"sources/{key}.md"}),
                ("workspace_write_file", {"path": f"{key}.md", "content": f"分析 {key}。"}),
                envelope_step(
                    summary=f"候选 {key}",
                    artifacts=[f"{key}.md"],
                    claims=["主张"],
                    override=lambda body: self.cited(body, key),
                ),
            ]

        def finish_synthesis(request):
            # The actual Synthesizer request is the only source of these IDs.
            knowledge_ids = [item["id"] for item in package_of(request)["verified_knowledge"]]
            return envelope_step(
                summary="条件综合",
                artifacts=["s.md"],
                claims=["综合"],
                override=lambda body: {
                    **self.cited(body, "s"),
                    "used_knowledge": knowledge_ids,
                },
            )(request)

        return RoleScriptedProvider(
            {
                "worker": work("a") + work("b"),
                "arbiter": [
                    ("workspace_read_file", {"path": "sources/a.md"}),
                    ("workspace_read_file", {"path": "sources/b.md"}),
                    (
                        "workspace_write_file",
                        {
                            "path": "arbitration/world.offline/report.md",
                            "content": "条件不同，请人工审阅。",
                        },
                    ),
                    envelope_step(
                        summary="交由人判断",
                        artifacts=["arbitration/world.offline/report.md"],
                        claims=["建议"],
                        override=lambda body: self.cited(body, "arb"),
                    ),
                ],
                "synthesizer": [
                    ("workspace_read_file", {"path": "sources/a.md"}),
                    ("workspace_read_file", {"path": "sources/b.md"}),
                    (
                        "workspace_write_file",
                        {
                            "path": "s.md",
                            "content": "两种资料的适用条件不同。",
                        },
                    ),
                    finish_synthesis,
                ],
                "critic": [
                    critic_step(verdict="PASS", criteria_met=True),
                    critic_step(verdict="PASS", criteria_met=True),
                ],
            }
        )

    @staticmethod
    def _ordinary(label: str) -> RoleScriptedProvider:
        content = f"# {label}\nActual pressure fixture.\n"

        def assessed(request):
            reads = [
                json.loads(message.content)
                for message in request.messages
                if str(message.role) == "tool"
            ]
            met = reads[-1]["value"]["content"] == content
            return critic_step(verdict="PASS" if met else "FAIL", criteria_met=met)(request)

        return RoleScriptedProvider(
            {
                "worker": [
                    ("workspace_write_file", {"path": "a.md", "content": content}),
                    envelope_step(summary=label, artifacts=["a.md"], claims=[content]),
                ],
                "critic": [("workspace_read_file", {"path": "a.md"}), assessed],
            }
        )

    def count(self, label: str, role: str) -> int:
        return sum(
            current_label == label and current_role == role
            for current_label, current_role, _request_id in self.calls
        )

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        package = package_of(request)
        label = package["mission_root_goal"]
        role = role_of(request)
        self.calls.append((label, role, str(request.request_id)))
        if label == "doc" and role == "synthesizer":
            self.synthesis_package_ids.append(
                tuple(item["id"] for item in package["verified_knowledge"])
            )
        if (
            role == "worker"
            and label == "doc"
            and (package.get("task_contract") or {}).get("task_id") == self.b_task_id
        ):
            self.b_entered.set()
            await self.release_b.wait()
        if role == "worker" and label == "P2":
            self.p2_entered.set()
            await self.release_p2.wait()
        if role == "critic" and label in self.release_critic:
            self.critic_entered[label].set()
            await self.release_critic[label].wait()
        return await self.scripts[label].invoke(request, cancel=cancel)

    def release_all(self) -> None:
        self.release_b.set()
        self.release_p2.set()
        for event in self.release_critic.values():
            event.set()


async def until(predicate, runner: asyncio.Task[None]) -> None:
    async def poll() -> None:
        while not predicate():
            if runner.done():
                await runner
                raise AssertionError("Mission loop became idle before required phase")
            await asyncio.sleep(0.002)

    await asyncio.wait_for(poll(), 12)


async def ordinary_mission(orch: Orchestrator, label: str):
    mission = await orch.submit_mission(spec(label, goal=label, success_criteria=("file:a.md",)))
    planning = orch.commit.begin_planning(mission.id)
    tasks, _ = orch.commit.commit_task_graph(
        mission.id,
        TaskGraphProposal.from_json(
            {
                "tasks": [
                    node(
                        "A",
                        tokens=80_000,
                        verification_policy=["format_check", "rule_check", "critic_review"],
                    )
                ]
            }
        ),
        base_version=planning.version,
        source={"planner": "A03 public graph fixture"},
    )
    return mission, tasks[0]


def pressure_load(orch: Orchestrator, mission_ids: set[str]) -> tuple[int, int]:
    """PENDING/RUNNING Results plus submitted Attempts able to produce a Result."""
    pending = sum(
        result.envelope.mission_id in mission_ids
        for result in orch.store.list_results_by_verification("PENDING", "RUNNING")
    )
    potential = sum(
        intent.mission_id in mission_ids
        and intent.kind == "attempt"
        and orch.store.find_result_for_attempt(intent.subject_id) is None
        for intent in orch.store.list_intents("SUBMITTED")
    )
    return pending, potential


def test_a03_pressure_priority_drain_preserves_actual_conflict_and_final_tails(
    tmp_path, monkeypatch
):
    async def exercise() -> None:
        provider = CompositeProvider()
        config = OrchestratorConfig(
            evidence_root=tmp_path / "a03-priority-drain",
            max_concurrency=6,
            max_concurrent_model_calls=4,
            candidates_per_task=1,
            dynamic_graph=False,
            verifier_workers=1,
            max_pending_verifications=LIMIT,
            low_watermark_ratio=0.0,
            exploration_slots=0,
            reduced_concurrency_ratio=0.5,
        )
        async with Orchestrator(
            config, provider, provider_token_estimator=Counter(1000), poll_interval=0.002
        ) as orch:
            with monkeypatch.context() as patch:
                patch.setattr(
                    domains,
                    "DOMAINS",
                    {
                        **domains.DOMAINS,
                        DOC_DOMAIN: domains.DOC_PROFILE_V4,
                    },
                )
                mission = await orch.submit_mission(
                    spec(
                        "doc",
                        goal="doc",
                        domain=DOC_DOMAIN,
                        success_criteria=("file:s.md",),
                        conflict_reserve_tokens=20_000,
                        synthesis={
                            "goal": "综合两份有争议的资料",
                            "success_criteria": ["file:s.md"],
                            "outputs": ["s.md"],
                            "budget": {"max_tokens": 30_000, "max_attempts": 2},
                        },
                    )
                )
            api = MissionControlV1(
                orch, tenant_id=mission.tenant_id, principal=Principal("reviewer")
            )
            for key, quote in QUOTES.items():
                api.register_source(
                    {
                        "mission_id": mission.id,
                        "path": f"sources/{key}.md",
                        "content": quote + "\n",
                        "kind": "markdown",
                        "idempotency_key": key,
                    }
                )
                provider.versions[key] = orch.store.get_source(mission.id, f"sources/{key}.md")[
                    "version_hash"
                ]
            # Snapshot the original Mission pools before graph insertion moves
            # synthesis money into its actual Task hold.
            pools = {
                row["purpose"]: dict(row)
                for row in orch.store.connection.execute(
                    "SELECT * FROM mission_system_tail_pools WHERE mission_id=?", (mission.id,)
                )
            }
            assert set(pools) == {"conflict", "synthesis"}
            original = {
                purpose: orch.commit.ledger.reservation(row["subject_id"])["reserved_tokens"]
                for purpose, row in pools.items()
            }
            assert original == {"conflict": 20_000, "synthesis": 30_000}
            planning = orch.commit.begin_planning(mission.id)
            tasks, _ = orch.commit.commit_task_graph(
                mission.id,
                TaskGraphProposal.from_json(
                    {
                        "tasks": [
                            node(
                                "A",
                                tokens=40_000,
                                success_criteria=["file:a.md", "cite:sources/a.md"],
                            ),
                            node(
                                "B",
                                ["A"],
                                tokens=40_000,
                                success_criteria=["file:b.md", "cite:sources/b.md"],
                            ),
                        ]
                    }
                ),
                base_version=planning.version,
                source={"planner": "A03 document graph fixture"},
            )
            provider.b_task_id = next(task.id for task in tasks if task.goal == "任务 B")

            active_ids = {mission.id}
            runner = asyncio.create_task(orch.run())
            try:
                await until(lambda: provider.b_entered.is_set(), runner)
                assert orch.store.get_task(tasks[0].id).status is TaskStatus.COMPLETED
                p1, _ = await ordinary_mission(orch, "P1")
                active_ids.add(p1.id)
                await until(lambda: provider.critic_entered["P1"].is_set(), runner)
                p2, _ = await ordinary_mission(orch, "P2")
                active_ids.add(p2.id)
                await until(lambda: provider.p2_entered.is_set(), runner)
                assert sum(pressure_load(orch, active_ids)) == LIMIT
                provider.release_b.set()
                await until(lambda: pressure_load(orch, active_ids)[0] == 2, runner)
                provider.release_p2.set()
                await until(lambda: orch.pressure.is_raised, runner)
                assert sum(pressure_load(orch, active_ids)) <= LIMIT
                assert (
                    orch.store.get_scheduler_state("backpressure")["peaks"]["pending_verifications"]
                    <= LIMIT
                )

                fresh, _ = await ordinary_mission(orch, "fresh")
                active_ids.add(fresh.id)
                await asyncio.wait_for(asyncio.sleep(0.05), 1)
                assert provider.count("fresh", "worker") == 0
                assert orch.store.list_attempts(orch.store.list_tasks(fresh.id)[0].id) == []
                provider.release_critic["P1"].set()
                await until(lambda: provider.count("doc", "arbiter") > 0, runner)
                assert orch.pressure.is_raised
                [conflict] = orch.store.list_conflicts(mission.id)
                assert conflict["state"] == "OPEN"
                conflict_task = orch.store.get_task(conflict["task_id"])
                hold = orch.commit.system_task_hold(conflict_task.id)
                assert hold is not None and hold["pool_id"] == pools["conflict"]["pool_id"]
                assert orch.commit.protected_tail_hold(hold["hold_id"])["state"] == "HELD"
                assert sum(pressure_load(orch, active_ids)) <= LIMIT

                provider.release_critic["P2"].set()
                await until(
                    lambda: any(
                        row["kind"] == "arbitration" and row["state"] == "PENDING"
                        for row in orch.store.list_approvals(mission.id)
                    ),
                    runner,
                )
                [review] = [
                    row
                    for row in orch.store.list_approvals(mission.id)
                    if row["kind"] == "arbitration" and row["state"] == "PENDING"
                ]
                assert review["topic"] == "conflict"
                assert provider.count("doc", "critic") >= 1
                assert review["subject_key"] == conflict["conflict_id"]
                assert review["task_id"] == conflict_task.id
                binding = review["binding"]
                arbiter_result = orch.store.get_result(binding["result_id"])
                assert arbiter_result is not None
                assert arbiter_result.envelope.task_id == conflict_task.id
                assert arbiter_result.envelope.attempt_id == binding["attempt_id"]
                assert arbiter_result.verification_state == "SUSPENDED"
                verified_by_id = {
                    record.id: record.source_task
                    for record in orch.store.list_knowledge(mission.id)
                    if str(record.status) == "VERIFIED"
                }
                assert set(verified_by_id.values()) == {tasks[0].id, provider.b_task_id}
                assert len(verified_by_id) == 2
                await asyncio.wait_for(runner, 15)
                api.decide(
                    review["request_id"],
                    "arbitrate",
                    ruling="contextual",
                    basis="核对双方原文，仅在各自条件内适用。",
                    nonce="a03-contextual",
                )
                resumed = asyncio.create_task(orch.run())
                try:
                    await until(lambda: bool(provider.synthesis_package_ids), resumed)
                    selected_ids = provider.synthesis_package_ids[0]
                    assert len(selected_ids) == 2
                    assert set(selected_ids) == set(verified_by_id)
                    await asyncio.wait_for(resumed, 15)
                finally:
                    if not resumed.done():
                        resumed.cancel()
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(resumed, 2)
                assert orch.store.get_task(conflict_task.id).status is TaskStatus.CANCELLED
                assert provider.count("doc", "synthesizer") >= 4
                assert provider.count("doc", "critic") >= 2
                synthesis = next(
                    task for task in orch.store.list_tasks(mission.id) if task.kind == "synthesis"
                )
                assert synthesis.status is TaskStatus.COMPLETED
                result = orch.store.get_result(synthesis.accepted_result_id)
                assert result is not None and result.verdict == "PASS"
                assert provider.synthesis_package_ids
                assert all(ids == selected_ids for ids in provider.synthesis_package_ids)
                assert set(result.envelope.used_knowledge) == set(selected_ids)
                assert any(
                    layer["layer"] == "critic_review" and layer["status"] == "PASS"
                    for layer in orch.store.list_verifications(result.envelope.id)
                )
                assert orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
                assert orch.store.get_mission(fresh.id).status is MissionStatus.COMPLETED
                assert (
                    orch.store.get_scheduler_state("backpressure")["log"][-1]["level"] == "NORMAL"
                )
                assert (
                    orch.store.get_scheduler_state("backpressure")["peaks"]["pending_verifications"]
                    <= LIMIT
                )
                events = [
                    event
                    for mission_id in active_ids
                    for event in orch.store.list_events(mission_id)
                ]
                assert any(
                    event.type == "BackpressureRaised"
                    and event.payload["dimension"] == "pending_verifications"
                    for event in events
                )
                assert any(
                    event.type == "BackpressureCleared"
                    and event.payload["dimension"] == "pending_verifications"
                    for event in events
                )
                for purpose in ("conflict", "synthesis"):
                    row = orch.store.connection.execute(
                        "SELECT * FROM mission_system_tail_pools WHERE pool_id=?",
                        (pools[purpose]["pool_id"],),
                    ).fetchone()
                    assert row["state"] == "RELEASED"
                    [system] = orch.store.connection.execute(
                        "SELECT * FROM mission_system_tail_tasks WHERE pool_id=?",
                        (row["pool_id"],),
                    ).fetchall()
                    transfers = orch.store.connection.execute(
                        "SELECT transfer_id FROM budget_tail_transfers WHERE hold_id=?",
                        (system["hold_id"],),
                    ).fetchall()
                    subjects = [item[0] for item in transfers]
                    assert {
                        orch.store.get_intent_for_subject(subject).kind for subject in subjects
                    } == {"attempt", "critic"}
                    assert all(
                        orch.commit.ledger.reservation(subject)["state"] == "SETTLED"
                        for subject in subjects
                    )
                assert orch.commit.ledger.account("budget:" + mission.id).reserved_tokens == 0
            finally:
                provider.release_all()
                if not runner.done():
                    runner.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(runner, 2)

    asyncio.run(asyncio.wait_for(exercise(), 35))

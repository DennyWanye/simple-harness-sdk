# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""A03: provider-boundary pressure control around a slow real Verifier path.

Three Missions request Workers; admission must reserve room for their results. Their
real workspace writes, Result Envelopes and Critic verification therefore exercise
the in-flight-completion edge of ``max_pending_verifications``.  The test does not
inject a verdict or write scheduler state: it reads the persisted pressure events
and the Provider call trace produced by the production Mission loop.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step, package_of
from graph_helpers7 import node, spec
from test_provider_budget_guard import Counter

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.graph.task_graph import TaskGraphProposal
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.testing.fixtures import role_of

INITIAL = ("A", "B", "C")
LIMIT = 2


class PressureProvider:
    """Fixture Provider that gates pre-existing Workers and the real Critic route."""

    def __init__(self) -> None:
        self.release_workers = asyncio.Event()
        self.release_critics = asyncio.Event()
        self.calls: list[tuple[tuple[str, str], str]] = []
        self._scripts: dict[str, RoleScriptedProvider] = {}
        for label in (*INITIAL, "D"):
            report = f"# {label}\nVerifier-pressure fixture material.\n"

            def assessed(request, *, expected=report):
                reads = [
                    json.loads(message.content)
                    for message in request.messages
                    if str(message.role) == "tool"
                ]
                met = reads[-1]["value"]["content"] == expected
                return critic_step(verdict="PASS" if met else "FAIL", criteria_met=met)(request)

            self._scripts[label] = RoleScriptedProvider(
                {
                    "worker": [
                        ("workspace_write_file", {"path": "a.md", "content": report}),
                        envelope_step(
                            summary=f"wrote {label}",
                            artifacts=["a.md"],
                            claims=[f"a.md contains {label} fixture material"],
                        ),
                    ],
                    "critic": [("workspace_read_file", {"path": "a.md"}), assessed],
                }
            )

    def calls_for(self, label: str, role: str) -> int:
        return sum(current == (label, role) for current, _request in self.calls)

    async def invoke(self, request, *, cancel):  # type: ignore[no-untyped-def]
        label = package_of(request)["mission_root_goal"]
        role = role_of(request)
        self.calls.append(((label, role), str(request.request_id)))
        if role == "worker" and label in INITIAL:
            await self.release_workers.wait()
        if role == "critic":
            await self.release_critics.wait()
        return await self._scripts[label].invoke(request, cancel=cancel)


async def _until(predicate, runner: asyncio.Task[None]) -> None:  # type: ignore[type-arg]
    async def poll() -> None:
        while not predicate():
            if runner.done():
                await runner
                raise AssertionError("orchestrator became idle before the required phase")
            await asyncio.sleep(0.002)

    await asyncio.wait_for(poll(), 10)


async def _mission(orch: Orchestrator, label: str):
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
        source={"planner": "A03 controlled provider pressure fixture"},
    )
    return mission, tasks[0]


def test_a03_slow_verifier_bounds_inflight_results_and_resumes_worker_expansion(tmp_path):
    async def exercise() -> None:
        provider = PressureProvider()
        config = OrchestratorConfig(
            evidence_root=tmp_path / "verifier-pressure",
            max_concurrency=6,
            max_concurrent_model_calls=3,
            candidates_per_task=1,
            dynamic_graph=False,
            verifier_workers=1,
            max_pending_verifications=LIMIT,
            low_watermark_ratio=0.5,
            reduced_concurrency_ratio=0.5,
        )
        async with Orchestrator(
            config, provider, provider_token_estimator=Counter(1000), poll_interval=0.002
        ) as orch:
            missions = {label: await _mission(orch, label) for label in INITIAL}
            runner = asyncio.create_task(orch.run())
            try:
                await _until(
                    lambda: (
                        sum(provider.calls_for(label, "worker") >= 1 for label in INITIAL) >= LIMIT
                    ),
                    runner,
                )
                provider.release_workers.set()
                await _until(
                    lambda: (
                        orch.pressure.is_raised
                        and sum(provider.calls_for(label, "worker") == 2 for label in INITIAL)
                        >= LIMIT
                    ),
                    runner,
                )

                pressure_before_drain = orch.store.get_scheduler_state("backpressure")
                assert pressure_before_drain["limits"]["max_pending_verifications"] == LIMIT
                assert any(
                    event.type == "BackpressureRaised"
                    and event.payload["dimension"] == "pending_verifications"
                    for mission, _task in missions.values()
                    for event in orch.store.list_events(mission.id)
                )

                missions["D"] = await _mission(orch, "D")
                await asyncio.wait_for(asyncio.sleep(0.05), 1)
                # No new Worker may add a result to the occupied verification capacity.
                assert provider.calls_for("D", "worker") == 0

                provider.release_critics.set()
                await asyncio.wait_for(runner, 10)
                final_pressure = orch.store.get_scheduler_state("backpressure")
                assert final_pressure["log"][-1]["level"] == "NORMAL"
                assert all(
                    orch.store.get_mission(mission.id).status is MissionStatus.COMPLETED
                    for mission, _task in missions.values()
                )
                assert provider.calls_for("D", "worker") == 2

                # This is the A03 hard-bound oracle.  It includes the RUNNING Verifier
                # plus PENDING results from Workers that had already crossed Provider
                # admission when pressure was raised.
                assert final_pressure["peaks"]["pending_verifications"] <= LIMIT
            finally:
                provider.release_workers.set()
                provider.release_critics.set()
                if not runner.done():
                    runner.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(runner, 2)

    asyncio.run(asyncio.wait_for(exercise(), 10))

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""NEXT-TG-1.0 §9 "多任务并发": raising the model-call slot count keeps a library running.

The physical slot count is capacity, not the accounting identity of one frozen request:
the same estimator / protocol / prices account a request the same way whatever the slot
count.  2026-09-28 a Host raised it from 1 to 2 and the orchestration service refused to
start ("provider admission identity differs from a persisted intent"), because the slot
count was hashed into every frozen intent.  Here: Mission A completes and Mission B has a
frozen, never handed-off Planner request under one slot; the same library reopens with
two slots, B completes, and a new Mission C runs two model calls at once.
"""

from __future__ import annotations

import asyncio

from graph_helpers7 import spec
from test_provider_budget_guard import Counter
from test_queued_planner_cancel import PlannerLoad

from agent_orchestrator.contracts import MissionStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import OrchestratorConfig
from agent_orchestrator.runtime.provider_budget_guard import ProviderBudgetGuard


def _config(tmp_path, slots: int) -> OrchestratorConfig:
    return OrchestratorConfig(evidence_root=tmp_path, max_concurrency=3, max_concurrent_model_calls=slots,
                              candidates_per_task=1, dynamic_graph=False)


def _mission(label: str):
    return spec(label, goal=label, success_criteria=("file:a.md",))


async def _finish(orch, *mission_ids: str) -> list[MissionStatus]:
    """Drive the loop until every Mission ends (``run`` returns when idle)."""
    async def done():
        while any(orch.store.get_mission(m).status not in (MissionStatus.COMPLETED, MissionStatus.FAILED) for m in mission_ids):
            await orch.run(max_cycles=50)
            await asyncio.sleep(0.005)
    await asyncio.wait_for(done(), 40)
    return [orch.store.get_mission(m).status for m in mission_ids]


def test_the_slot_count_is_capacity_not_the_frozen_request_identity():
    one = ProviderBudgetGuard(_Commit(), owner="o", estimator=Counter(1), max_slots=1)
    two = ProviderBudgetGuard(_Commit(), owner="o", estimator=Counter(1), max_slots=2, profile_slots={"default": 2})
    assert one.fingerprint == two.fingerprint
    # a request frozen by an earlier build (the slot count was hashed in) is still this identity
    # byte-exact: the identity opt.84 froze for this estimator under one slot
    legacy = one.legacy_fingerprint(max_slots=1, profile_slots=None)
    assert legacy == "provider-budget-admission-v2:1c1ed19809e428fe86e141ada28e08017d7113cc9123622624a5e857f59f50a8"
    assert two.accepts(legacy)
    assert not two.accepts("provider-budget-admission-v2:" + "0" * 64)
    other = ProviderBudgetGuard(_Commit(), owner="o", estimator=_OtherCounter(1), max_slots=2)
    assert other.fingerprint != two.fingerprint and not other.accepts(one.legacy_fingerprint(max_slots=1, profile_slots=None))


def test_a_library_frozen_by_an_earlier_build_reopens_with_more_slots_and_runs_two_calls_at_once(tmp_path, monkeypatch):
    """Phase 1 is an earlier build (the slot count hashed into the identity) with one slot;
    phase 2 is this build with two: it starts, and two Missions call the model at once."""
    async def exercise():
        original = ProviderBudgetGuard._identity
        with monkeypatch.context() as patch:
            patch.setattr(ProviderBudgetGuard, "_identity",
                          lambda self, *, version, max_slots=None, profile_slots=None: original(self, version=2, max_slots=1))
            async with Orchestrator(_config(tmp_path, 1), PlannerLoad(gate_role="nobody"),
                                    provider_token_estimator=Counter(1000), poll_interval=0.002) as orch:
                first = await orch.submit_mission(_mission("A"))
                assert await _finish(orch, first.id) == [MissionStatus.COMPLETED]
                frozen = {str(i.config.get("provider_admission_fingerprint")) for i in orch.store.list_intents("SETTLED")}
                assert frozen and all(f.startswith("provider-budget-admission-v2:") for f in frozen)
        provider = PlannerLoad(gate_role="worker", gate_labels={"LONG", "B"})
        async with Orchestrator(_config(tmp_path, 2), provider, provider_token_estimator=Counter(1000),
                                poll_interval=0.002) as orch:  # starts: the v2 history is accepted
            both = [await orch.submit_mission(_mission(label)) for label in ("LONG", "B")]
            runner = asyncio.create_task(orch.run())
            try:
                async def two_in_flight():
                    while provider.active < 2:
                        await asyncio.sleep(0.002)
                await asyncio.wait_for(two_in_flight(), 20)
                assert provider.peak == 2
                provider.release.set()
            finally:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
            assert await _finish(orch, *(m.id for m in both)) == [MissionStatus.COMPLETED] * 2

    asyncio.run(exercise())


class _Commit:
    store = None


class _OtherCounter(Counter):
    fingerprint = "fixture-text-count-v2"

# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 2 · regressions for the independent code review (round 1).

P0-1 a Worker that rewrites its own acceptance test is caught; P1-2 a restart under a
live lease does not crash the loop; P1-3 workspace bindings are restored on recover;
P1-4 a stalled executor times out and is retried; P1-5 cancel closes intents and
releases reservations; P1-6 priced mode settles real money and a model-echo mismatch
holds the reservation and stops visibly; P2-8 a crash mid-commit leaves nothing behind.
"""

from __future__ import annotations

import asyncio

import pytest
from fixtures_provider import RoleScriptedProvider, critic_step, envelope_step, proposal_step
from test_recovery_matrix import _crash_then_recover, _provider
from test_single_task_closure import GOOD, PROPOSAL, config, spec, worker_script

from agent_orchestrator.contracts import AttemptStatus, MissionStatus, TaskStatus
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.runtime.assembly import PriceTable
from agent_orchestrator.storage.store import InjectedCrash

FAKE_TEST = "def test_nothing():\n    assert True\n"


def test_p0_worker_cannot_rewrite_its_own_acceptance_test(tmp_path):
    cheat = [
        (
            "workspace_write_file",
            {"path": "parse_kv.py", "content": "def parse_kv(text):\n    return 'not a dict'\n"},
        ),
        ("workspace_write_file", {"path": "tests/test_parse_kv.py", "content": FAKE_TEST}),
        ("run_tests", {"path": "tests/test_parse_kv.py"}),  # passes in the Worker's own tree
        envelope_step(summary="done", artifacts=["parse_kv.py"], claims=["tests pass"]),
    ]
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": cheat + worker_script(GOOD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
        }
    )

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m-p0"))
            await orchestrator.run()
            store = orchestrator.store
            task = store.list_tasks(mission.id)[0]
            attempts = store.list_attempts(task.id)
            first = store.find_result_for_attempt(attempts[0].id)
            assert first.verdict == "FAIL"
            layers = {v["layer"]: v for v in store.list_verifications(first.envelope.id)}
            assert layers["rule_check"]["status"] == "FAIL"
            assert "protected seed file rewritten" in layers["rule_check"]["detail"]["summary"]
            # the real acceptance test was re-materialised in the verification copy
            copy = (
                orchestrator.config.workspaces_root
                / f"{attempts[0].id}-verify"
                / "tests"
                / "test_parse_kv.py"
            )
            assert "test_empty" in copy.read_text()
            # the repair attempt starts from the real tests again and passes
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED
            assert attempts[1].status is AttemptStatus.COMPLETED
            repaired = (
                orchestrator.config.workspaces_root / attempts[1].id / "tests" / "test_parse_kv.py"
            )
            assert "test_empty" in repaired.read_text()

    asyncio.run(case())


def test_p1_restart_under_a_live_lease_does_not_crash_and_resumes_later(tmp_path):
    provider = _provider()

    async def case():
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.6), provider, owner="orch-1"
        ) as first:
            mission = await first.submit_mission(spec("m-lease"))
            first.arm_fault("after_submit", kind="attempt")
            with pytest.raises(InjectedCrash):
                await first.run()
        # restart immediately: the dead owner's lease is still live
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.6), provider, owner="orch-2"
        ) as second:
            await second.run(max_cycles=5, until_idle=False)  # must not raise
            attempt = second.store.list_attempts(second.store.list_tasks(mission.id)[0].id)[0]
            assert attempt.status in {AttemptStatus.CLAIMED, AttemptStatus.RUNNING}
            assert attempt.lease_owner == "orch-1"  # the live lease was not preempted
            # bindings were restored for the resumed turn (P1-3)
            assert second.assembled.gateway.binding_for(attempt.agent_id) is not None
            await asyncio.sleep(0.7)
            await second.run()
            assert second.store.get_mission(mission.id).status is MissionStatus.COMPLETED

    asyncio.run(case())


def test_p1_stalled_executor_times_out_and_is_retried(tmp_path):
    gate = asyncio.Event()
    provider = RoleScriptedProvider(
        {
            "planner": [proposal_step(PROPOSAL)],
            "worker": worker_script(GOOD) + worker_script(GOOD),
            "critic": [critic_step(verdict="PASS", criteria_met=True)] * 2,
        },
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, lease_seconds=0.2, stall_seconds=0.3), provider, poll_interval=0.02
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m-stall"))
            # let planning finish, then stall the worker's first model call
            while not orchestrator.store.list_tasks(mission.id):
                await orchestrator.run(max_cycles=1, until_idle=False)
                await asyncio.sleep(0.02)
            provider.gate = gate
            deadline = asyncio.get_running_loop().time() + 5
            timed_out = False
            while asyncio.get_running_loop().time() < deadline:
                await orchestrator.run(max_cycles=1, until_idle=False)
                await asyncio.sleep(0.05)
                attempts = orchestrator.store.list_attempts(
                    orchestrator.store.list_tasks(mission.id)[0].id
                )
                if attempts and attempts[0].status is AttemptStatus.TIMED_OUT:
                    timed_out = True
                    break
            assert timed_out, orchestrator.progress_log
            assert attempts[0].failure["reason"] == "executor_stalled"
            assert orchestrator.store.count_events(mission.id, "AttemptTimedOut") == 1
            provider.gate = None  # the stalled call stays stuck; the retry runs normally
            await orchestrator.run()
            final = orchestrator.store.get_mission(mission.id)
            attempts = orchestrator.store.list_attempts(
                orchestrator.store.list_tasks(mission.id)[0].id
            )
            assert attempts[1].retry_of == attempts[0].id
            assert final.status is MissionStatus.COMPLETED, orchestrator.progress_log

    asyncio.run(case())


def test_p1_cancel_closes_intents_and_releases_reservations(tmp_path):
    provider = _provider()

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m-cancel"))
            while not orchestrator.store.list_tasks(mission.id):  # planned
                await orchestrator.run(max_cycles=1, until_idle=False)
                await asyncio.sleep(0.02)
            worker_calls = provider.by_role.get("worker", 0)
            orchestrator.commit.cancel_mission(mission.id)
            await orchestrator.run()  # must not dispatch, must not raise
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.CANCELLED
            assert store.list_tasks(mission.id)[0].status is TaskStatus.CANCELLED
            assert store.list_intents("PENDING", "CLAIMED", "AGENT_CREATED", "SUBMITTED") == []
            with store.transaction():
                report = orchestrator.commit.ledger.costs_report(mission.id)
            assert all(r["state"] == "SETTLED" for r in report["reservations"]), report[
                "reservations"
            ]
            assert all(a["reserved_tokens"] == 0 for a in report["accounts"])
            assert provider.by_role.get("worker", 0) == worker_calls  # nothing new was spent
            types = [e.type for e in store.list_events(mission.id)]
            assert "MissionCancelled" in types and "TaskCancelled" in types

    asyncio.run(case())


def test_p1_priced_mode_settles_real_money(tmp_path):
    provider = _provider()
    price = PriceTable(
        snapshot_id="test",
        input_micros_per_million_tokens=1_000_000,
        output_micros_per_million_tokens=2_000_000,
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, price_table=price, hard_cap_micros=50_000_000), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m-priced"))
            await orchestrator.run()
            store = orchestrator.store
            assert store.get_mission(mission.id).status is MissionStatus.COMPLETED
            with store.transaction():
                report = orchestrator.commit.ledger.costs_report(mission.id)
            account = next(a for a in report["accounts"] if a["scope"] == "mission")
            assert account["settled_cost_micros"] > 0 and account["unpriced_settlements"] == 0
            assert account["reserved_cost_micros"] == 0
            assert all(u["unpriced"] == 0 and u["cost_micros"] > 0 for u in report["usage"])

    asyncio.run(case())


def test_p1_model_echo_mismatch_holds_reservation_and_stops(tmp_path):
    provider = RoleScriptedProvider(
        {"planner": [proposal_step(PROPOSAL)], "worker": worker_script(GOOD), "critic": []},
        model="other-model",
    )
    price = PriceTable(
        snapshot_id="test",
        input_micros_per_million_tokens=1_000_000,
        output_micros_per_million_tokens=2_000_000,
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, price_table=price, hard_cap_micros=None), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(spec("m-echo"))
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED
            assert final.stop_reason in {"model_echo_mismatch", "planning_failed"}, (
                orchestrator.progress_log
            )
            with store.transaction():
                report = orchestrator.commit.ledger.costs_report(mission.id)
            # unknown charges are imported as unknown (never zero) and keep their reservation
            assert any(u["unknown"] == 1 for u in report["usage"])
            held = [r for r in report["reservations"] if r["state"] == "RESERVED"]
            assert held, report["reservations"]
            assert all(
                r["settled_cost_micros"] != 0
                for r in report["reservations"]
                if r["state"] == "SETTLED"
            )

    asyncio.run(case())


def test_p2_crash_mid_commit_leaves_nothing_behind(tmp_path):
    crashed, worker_calls, provider, sdk, final = _crash_then_recover(tmp_path, "mid_commit")
    assert crashed["results"] == []  # the transaction rolled back
    assert final["results"][0]["verdict"] == "PASS" and provider.by_role["worker"] == 5

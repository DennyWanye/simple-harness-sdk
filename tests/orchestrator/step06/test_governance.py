# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
# ruff: noqa: E501

"""Step 6 · S6-07 (D6-8): when one budget dimension — tokens, tool calls or wall-clock —
is exhausted, no new Attempt is allocated and the ledger keeps what was spent and what
is still reserved, without double counting."""

from __future__ import annotations

import asyncio

from helpers_step06 import config, events_of, only, spec

from agent_orchestrator.contracts import Budget, MissionStatus, TaskStatus
from agent_orchestrator.governance.budgets import UsageFact
from agent_orchestrator.orchestrator.commit_service import mission_account
from agent_orchestrator.orchestrator.event_handler import Orchestrator
from agent_orchestrator.testing.fixtures import (
    _recorder_task,
    _write_files_then,
    demo_dynamic_dag_provider,
    graph_change_step,
    recorder_scripts,
)


def _implementation_task(**overrides):
    task = _recorder_task(
        "A",
        "实现 recorder.py 并通过 tests/test_recorder.py（独立任务）",
        [],
        ["pytest:tests/test_recorder.py"],
        3.0,
        ["recorder.py"],
        policy=["format_check", "rule_check", "code_test"],
    )
    task.update(overrides)
    return task


def _wrong():
    return _write_files_then(
        {"recorder.py": "def parse_line(line):\n    return {}\n"},
        test_path="tests/test_recorder.py",
        summary="实现完成",
        claim="tests/test_recorder.py 通过",
    )


def _ledger(orchestrator, mission_id):
    with orchestrator.store.transaction():
        return orchestrator.commit.ledger.account(mission_account(mission_id))


# ------------------------------------------------------------------ tool calls
def test_s6_07_tool_call_dimension_stops_new_allocation_and_keeps_the_books(tmp_path):
    # every wrong Attempt executes 3 tool calls (list, write, run_tests); the Mission allows 6
    provider = demo_dynamic_dag_provider(
        tasks=[_implementation_task()],
        per_attempt={"A": [_wrong(), _wrong(), recorder_scripts()["B2"]]},
        manager_steps=[graph_change_step([])] * 3,  # the step-5 failures trigger: "keep"
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, max_tool_calls_per_turn=3, manager_after_failures=10), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "tool-calls",
                    success_criteria=("pytest:tests/test_recorder.py",),
                    budget=Budget(max_tool_calls=6, max_attempts=8),
                )
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "budget_exhausted"
            assert final.final_report["detail"]["dimension"] == "tool_calls"
            assert (
                len(events_of(store, mission.id, "AttemptCreated")) == 2
            )  # the third was never allocated
            account = _ledger(orchestrator, mission.id)
            assert account.settled_tool_calls == 6 and account.reserved_tool_calls == 0
            assert account.remaining_tool_calls() == 0
            report = orchestrator.commit.ledger.costs_report(mission.id)
            assert sum(int(r["settled_tool_calls"] or 0) for r in report["reservations"]) == 6
            assert all(r["state"] == "SETTLED" for r in report["reservations"])
            # review P1-5: (1) a usage_ref lands once, (2) settled == Σ imported usage, (3) reserved ≥ 0 and
            # every open reservation is unsettled
            with store.transaction():
                dup = orchestrator.commit.ledger.import_usage(
                    subject_id=report["usage"][0]["subject_id"],
                    mission_id=mission.id,
                    facts=[UsageFact(report["usage"][0]["usage_ref"], 999, 999, None)],
                )
            assert dup == 0
            assert account.settled_tokens == sum(
                int(u["input_tokens"]) + int(u["output_tokens"]) for u in report["usage"]
            )
            assert account.reserved_tokens >= 0 and account.reserved_tool_calls >= 0
            assert not [r for r in report["reservations"] if r["state"] != "SETTLED"]
            released = events_of(store, mission.id, "BudgetReleased")
            assert [
                e.payload["settled_tool_calls"]
                for e in released
                if e.payload["subject_id"].endswith(("attempt-1", "attempt-2"))
            ] == [3, 3]

    asyncio.run(case())


def test_s6_07_the_gateway_enforces_the_reserved_tool_call_cap_per_attempt(tmp_path):
    """§21.1 step 4 at the gateway itself: the SDK's per-turn limit is set to the same cap
    (so a compliant runtime never reaches it) — the gateway is the authority when it does."""

    from agent_orchestrator.artifacts.workspace import WorkspaceManager
    from agent_orchestrator.runtime.tool_gateway import WorkspaceBinding, WorkspaceToolGateway
    from simple_harness.contracts import CallId
    from simple_harness.tools import ToolCall

    workspaces = WorkspaceManager(tmp_path / "ws")
    workspaces.create("m:task-1:attempt-1", seed={"a.md": "x"})
    gateway = WorkspaceToolGateway(workspaces)
    gateway.bind(
        "run-1",
        WorkspaceBinding(
            "m:task-1:attempt-1",
            "work",
            True,
            ("workspace_list", "workspace_read_file"),
            max_tool_calls=2,
        ),
    )

    async def case():
        results = []
        for n in range(3):
            call = ToolCall(call_id=CallId(f"c{n}"), name="workspace_list", arguments={})
            results.append(await gateway.execute(call, {"run_id": "run-1"}))
        return results

    first, second, third = asyncio.run(case())
    assert first.error_code is None and second.error_code is None
    assert third.error_code == "tool_rate_limited"
    assert gateway.executed_calls("run-1") == 2
    assert [c["outcome"] for c in gateway.calls] == [
        "succeeded",
        "succeeded",
        "rejected:rate_limited",
    ]


def test_s6_07_the_attempt_cap_is_the_narrower_of_deployment_and_task_dimensions(tmp_path):
    provider = demo_dynamic_dag_provider(
        tasks=[
            _implementation_task(
                budget={"max_tokens": 30_000, "max_attempts": 3, "max_tool_calls": 2}
            )
        ],
        per_attempt={"A": [recorder_scripts()["B2"]]},
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, max_tool_calls_per_turn=48), provider
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "tool-cap",
                    success_criteria=("pytest:tests/test_recorder.py",),
                    budget=Budget(max_tool_calls=20, max_attempts=8),
                )
            )
            await orchestrator.run()
            store = orchestrator.store
            first = store.list_attempts(store.list_tasks(mission.id)[0].id)[0]
            intent = store.get_intent_for_subject(first.id)
            assert (
                intent.config["max_tool_calls"] == 2
            )  # Task dimension narrows the deployment's 48
            assert intent.config["agent_config"]["limits"]["max_tool_calls_per_turn"] == 2
            reserved = orchestrator.commit.ledger.costs_report(mission.id)["reservations"]
            assert [r["reserved_tool_calls"] for r in reserved if r["subject_id"] == first.id] == [
                2
            ]

    asyncio.run(case())


# ------------------------------------------------------------------ wall clock
def test_s6_07_runtime_dimension_stops_new_allocation_after_the_mission_clock_runs_out(tmp_path):
    gate = asyncio.Event()
    provider = demo_dynamic_dag_provider(
        tasks=[_implementation_task()],
        per_attempt={"A": [_wrong(), recorder_scripts()["B2"]]},
        holds={"A": [gate, None]},
        manager_steps=[graph_change_step([])] * 3,
    )

    async def case():
        async with Orchestrator(
            config(tmp_path, manager_after_failures=10), provider, poll_interval=0.02
        ) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "runtime",
                    success_criteria=("pytest:tests/test_recorder.py",),
                    budget=Budget(max_runtime_seconds=1, max_attempts=8),
                )
            )
            loop = asyncio.get_running_loop()
            loop.call_later(1.1, gate.set)  # the first Attempt is held past the Mission's clock
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "budget_exhausted"
            assert final.final_report["detail"]["dimension"] == "runtime"
            assert (
                len(events_of(store, mission.id, "AttemptCreated")) == 1
            )  # no second Attempt after the clock ran out
            account = _ledger(orchestrator, mission.id)
            assert (
                account.settled_tokens > 0 and account.reserved_tokens == 0
            )  # the first Attempt's cost stayed on the books
            assert store.list_tasks(mission.id)[0].status in {
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }  # closed by the Mission-level stop

    asyncio.run(case())


# ------------------------------------------------------------------ tokens (the step-2 path, re-asserted here)
def test_s6_07_token_dimension_keeps_reserved_and_settled_consistent(tmp_path):
    provider = demo_dynamic_dag_provider(tasks=only("A"))

    async def case():
        async with Orchestrator(config(tmp_path), provider) as orchestrator:
            mission = await orchestrator.submit_mission(
                spec(
                    "tokens",
                    success_criteria=("file:analysis.md",),
                    budget=Budget(max_tokens=100, max_attempts=4),
                )
            )
            await orchestrator.run()
            store = orchestrator.store
            final = store.get_mission(mission.id)
            assert final.status is MissionStatus.FAILED and final.stop_reason == "budget_exhausted"
            assert final.final_report["detail"]["dimension"] == "tokens"
            assert not events_of(store, mission.id, "AttemptCreated")
            assert (
                final.final_report["detail"]["phase"] == "planning"
            )  # not even the Planner could be funded
            account = _ledger(orchestrator, mission.id)
            assert (
                account.reserved_tokens == 0
                and account.settled_tokens == 0
                and account.attempts_created == 0
            )
            assert orchestrator.commit.ledger.costs_report(mission.id)["reservations"] == []

    asyncio.run(case())

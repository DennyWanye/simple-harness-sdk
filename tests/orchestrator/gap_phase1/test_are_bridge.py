# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pinned official ARE objects, scripted runner; no provider or judge calls."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

pytest.importorskip("are")

from are.simulation.agents.agent_execution_result import AgentExecutionResult
from are.simulation.agents.are_simulation_agent import RunnableARESimulationAgent
from are.simulation.apps.app import App
from are.simulation.notification_system import (
    BaseNotificationSystem,
    Message,
    MessageType,
)
from are.simulation.scenarios.scenario import Scenario
from are.simulation.time_manager import TimeManager
from are.simulation.tool_utils import app_tool, data_tool, env_tool, user_tool
from are.simulation.types import OracleEvent

from agent_orchestrator.evaluation.are_bridge import (
    AREWorkerStillRunning,
    SimpleHarnessAREAgent,
)


class VisibleApp(App):
    def __init__(self) -> None:
        super().__init__(name="visible_app")
        self.oracle_secret = "hidden-app-state"

    @app_tool()
    def read_public(self) -> str:
        """Read an agent-visible value."""
        return "visible-value"

    @user_tool()
    def user_only(self) -> str:
        """User-only tool."""
        return "hidden-user-tool"

    @env_tool()
    def env_only(self) -> str:
        """Environment-only tool."""
        return "hidden-env-tool"

    @data_tool()
    def data_only(self) -> str:
        """Data-only tool."""
        return "hidden-data-tool"

    def get_state(self) -> dict[str, str]:
        return {"oracle_secret": self.oracle_secret}


def _scenario(*, turns: int | None = 1) -> Scenario:
    return Scenario(
        scenario_id="public-id",
        apps=[VisibleApp()],
        events=[OracleEvent(event_id="hidden-oracle-event")],
        comment="hidden-judge-comment",
        additional_system_prompt="public-system-prompt",
        start_time=900.0,
        nb_turns=turns,
    )


def _notifications() -> tuple[BaseNotificationSystem, TimeManager]:
    clock = TimeManager()
    clock.reset(start_time=1_000.0)
    system = BaseNotificationSystem()
    system.initialize(clock)
    return system, clock


def _put(system: BaseNotificationSystem, kind: MessageType, text: str, at: float) -> None:
    system.message_queue.put(Message(kind, text, datetime.fromtimestamp(at, tz=UTC)))


def test_official_contract_only_projects_agent_visible_fields() -> None:
    system, clock = _notifications()
    _put(system, MessageType.ENVIRONMENT_NOTIFICATION, "later", 1_002)
    _put(system, MessageType.USER_MESSAGE, "first", 1_001)
    clock.add_offset(2)

    class ScriptedRunner:
        def run_turn(self, *, notifications, state, tools, stop_event):
            assert not stop_event.is_set()
            assert [(n.kind, n.text, n.timestamp.timestamp()) for n in notifications] == [
                ("USER_MESSAGE", "first", 1_001),
                ("ENVIRONMENT_NOTIFICATION", "later", 1_002),
            ]
            assert state.simulated_time.timestamp() >= 1_002
            assert state.scenario_start_time.timestamp() == 900
            assert state.additional_system_prompt == "public-system-prompt"
            assert [d.name for d in tools.descriptions] == ["visible_app__read_public"]
            assert tools.invoke("visible_app__read_public", {}) == "visible-value"
            with pytest.raises(KeyError):
                tools.invoke("visible_app__env_only", {})
            assert "hidden-oracle-event" not in repr((notifications, state, tools.descriptions))
            assert "hidden-app-state" not in repr((notifications, state, tools.descriptions))
            assert "hidden-judge-comment" not in repr((notifications, state, tools.descriptions))
            return "scripted-answer"

    agent = SimpleHarnessAREAgent(ScriptedRunner())
    assert isinstance(agent, RunnableARESimulationAgent)
    result = agent.run_scenario(_scenario(), system)
    assert isinstance(result, AgentExecutionResult)
    assert result.output == "scripted-answer"
    assert result.metadata == {"terminal_reason": "max_turns", "turns": 1}


def test_environment_stop_preempts_entire_due_batch_including_earlier_messages() -> None:
    system, clock = _notifications()
    _put(system, MessageType.USER_MESSAGE, "must-not-run", 1_000)
    _put(system, MessageType.ENVIRONMENT_NOTIFICATION, "retained-in-queue", 1_000)
    _put(system, MessageType.ENVIRONMENT_STOP, "environment ended", 1_001)
    clock.add_offset(2)

    class ForbiddenRunner:
        def run_turn(self, **kwargs):
            pytest.fail("runner called after ENVIRONMENT_STOP")

    result = SimpleHarnessAREAgent(ForbiddenRunner()).run_scenario(_scenario(), system)
    assert result.output is None
    assert result.metadata == {"terminal_reason": "environment_stop", "turns": 0}
    assert [(m.message_type, m.message) for m in system.message_queue.list_view()] == [
        (MessageType.ENVIRONMENT_NOTIFICATION, "retained-in-queue")
    ]


def test_sync_stop_joins_cooperative_runner_without_residual_worker() -> None:
    system, _ = _notifications()
    _put(system, MessageType.USER_MESSAGE, "block", 1_000)
    entered = threading.Event()

    class BlockingRunner:
        def run_turn(self, *, stop_event, **kwargs):
            entered.set()
            assert stop_event.wait(2)
            return "late-answer"

    agent = SimpleHarnessAREAgent(BlockingRunner())
    results: list[AgentExecutionResult] = []
    worker = threading.Thread(
        target=lambda: results.append(agent.run_scenario(_scenario(), system))
    )
    worker.start()
    try:
        assert entered.wait(2)
        assert not agent.join(0.01)
        agent.stop()
        assert agent.join(2)
        assert not worker.is_alive()
        assert results[0].metadata == {"terminal_reason": "stopped", "turns": 0}
    finally:
        agent.stop()
        worker.join(2)


def test_async_cancel_stops_and_joins_cooperative_runner() -> None:
    system, _ = _notifications()
    _put(system, MessageType.USER_MESSAGE, "block", 1_000)
    entered = threading.Event()

    class BlockingRunner:
        def run_turn(self, *, stop_event, **kwargs):
            entered.set()
            assert stop_event.wait(2)
            return None

    agent = SimpleHarnessAREAgent(BlockingRunner())

    async def exercise() -> None:
        task = asyncio.create_task(agent.arun_scenario(_scenario(), system))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert agent.join(1)
            assert not any(
                t.name == "AREBridgeWorker" and t.is_alive() for t in threading.enumerate()
            )
        finally:
            agent.stop()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_async_cancel_reports_unjoined_runner_then_release_cleans_it() -> None:
    system, _ = _notifications()
    _put(system, MessageType.USER_MESSAGE, "block", 1_000)
    entered = threading.Event()
    release = threading.Event()

    class NonCooperativeRunner:
        def run_turn(self, **kwargs):
            entered.set()
            assert release.wait(2)
            return "late-answer"

    agent = SimpleHarnessAREAgent(NonCooperativeRunner())

    async def exercise() -> None:
        task = asyncio.create_task(agent.arun_scenario(_scenario(), system, join_timeout=0.01))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(AREWorkerStillRunning):
                await task
            assert not agent.join(0.01)
        finally:
            release.set()
            agent.stop()
            assert await asyncio.to_thread(agent.join, 2)

    asyncio.run(exercise())


def test_runner_is_required_and_log_replay_is_explicitly_rejected() -> None:
    with pytest.raises(TypeError):
        SimpleHarnessAREAgent(None)  # type: ignore[arg-type]
    system, _ = _notifications()

    class ScriptedRunner:
        def run_turn(self, **kwargs):
            return "unused"

    with pytest.raises(NotImplementedError):
        SimpleHarnessAREAgent(ScriptedRunner()).run_scenario(
            _scenario(), system, initial_agent_logs=[object()]
        )


def test_sync_exception_marks_run_complete_without_waiting_for_pool_thread_exit() -> None:
    system, _ = _notifications()
    _put(system, MessageType.USER_MESSAGE, "fail", 1_000)

    class FailingRunner:
        def run_turn(self, **kwargs):
            raise RuntimeError("script failure")

    agent = SimpleHarnessAREAgent(FailingRunner())
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="AREBridgePool") as pool:
        future = pool.submit(agent.run_scenario, _scenario(), system)
        with pytest.raises(RuntimeError, match="script failure"):
            future.result(timeout=2)
        assert agent.join(0)
        assert any(
            t.name.startswith("AREBridgePool") and t.is_alive() for t in threading.enumerate()
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), True, False])
def test_poll_and_join_reject_nonfinite_or_bool_seconds(bad: float) -> None:
    class ScriptedRunner:
        def run_turn(self, **kwargs):
            return None

    with pytest.raises((TypeError, ValueError)):
        SimpleHarnessAREAgent(ScriptedRunner(), poll_seconds=bad)
    agent = SimpleHarnessAREAgent(ScriptedRunner())
    with pytest.raises((TypeError, ValueError)):
        agent.join(bad)

    async def exercise() -> None:
        system, _ = _notifications()
        with pytest.raises((TypeError, ValueError)):
            await agent.arun_scenario(_scenario(), system, join_timeout=bad)

    asyncio.run(exercise())

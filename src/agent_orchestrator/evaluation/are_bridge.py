# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Bounded, oracle-free ARE Runnable agent boundary for an injected runner.

This is a contract bridge, not an Orchestrator runner or an ARE judge.  The host
retains the official Scenario; the injected runner receives only immutable
notification, clock and agent-tool projections.  ARE remains responsible for
environment events and its simulated clock.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

# ARE is an isolated, optional benchmark dependency; tested against its pinned runtime.
from are.simulation.agents.agent_execution_result import (  # type: ignore[import-not-found]
    AgentExecutionResult,
)
from are.simulation.agents.agent_log import BaseAgentLog  # type: ignore[import-not-found]
from are.simulation.agents.are_simulation_agent import (  # type: ignore[import-not-found]
    RunnableARESimulationAgent,
)
from are.simulation.notification_system import (  # type: ignore[import-not-found]
    BaseNotificationSystem,
    MessageType,
)
from are.simulation.scenarios.scenario import Scenario  # type: ignore[import-not-found]
from are.simulation.tool_utils import AppTool  # type: ignore[import-not-found]


@dataclass(frozen=True)
class ARENotification:
    kind: str
    text: str
    timestamp: datetime


@dataclass(frozen=True)
class AREVisibleState:
    """ARE-visible clock and prompt; app contents must be read via agent tools."""

    simulated_time: datetime
    scenario_start_time: datetime
    additional_system_prompt: str | None


@dataclass(frozen=True)
class AREToolDescription:
    name: str
    description: str
    arguments: tuple[tuple[str, str, str], ...]
    # Official agent-facing argument metadata, including optional/default values.
    argument_details: tuple[str, ...] = ()


class AREToolPort:
    """The official scenario's agent tools, with no user/env/data tool exposure."""

    def __init__(
        self, tools: list[AppTool], stopped: threading.Event,
        poll: Callable[[], tuple[ARENotification, ...]] | None = None,
        visible_state: Callable[[], AREVisibleState] | None = None,
    ) -> None:
        self._stopped = stopped
        self._poll = poll
        self._visible_state = visible_state
        self._tools: dict[str, AppTool] = {}
        descriptions = []
        for tool in tools:
            public_name = tool._public_name
            if not public_name or public_name in self._tools:
                raise ValueError("ARE agent tools require unique public names")
            self._tools[public_name] = tool
            descriptions.append(
                AREToolDescription(
                    name=public_name,
                    description=tool._public_description or "",
                    argument_details=tuple(str(arg) for arg in tool.args),
                    arguments=tuple(
                        (arg.name, str(arg.arg_type), arg.description or "") for arg in tool.args
                    ),
                )
            )
        self.descriptions = tuple(descriptions)

    def poll_notifications(self) -> tuple[ARENotification, ...]:
        """Consume only agent-visible messages collected while a turn is running."""
        return self._poll() if self._poll is not None else ()

    def visible_state(self) -> AREVisibleState:
        if self._visible_state is None:
            raise RuntimeError("ARE clock projection is unavailable")
        return self._visible_state()

    def invoke(self, name: str, arguments: Mapping[str, object]) -> object:
        if self._stopped.is_set():
            raise RuntimeError("ARE bridge stopped")
        tool = self._tools[name]
        result = tool(**dict(arguments))
        if self._stopped.is_set():
            raise RuntimeError("ARE bridge stopped during tool call")
        return result


class ARERunner(Protocol):
    """Explicit host port. A production implementation must drive the Orchestrator."""

    def run_turn(
        self,
        *,
        notifications: tuple[ARENotification, ...],
        state: AREVisibleState,
        tools: AREToolPort,
        stop_event: threading.Event,
    ) -> str | None: ...


class AREWorkerStillRunning(RuntimeError):
    """Cancellation was requested but the runner did not exit within the join bound."""


def _finite_seconds(name: str, value: float, *, allow_zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number of seconds")
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be finite and positive")


class SimpleHarnessAREAgent(RunnableARESimulationAgent):
    """Named ARE entry point; construction requires a real, explicit runner port."""

    def __init__(
        self, runner: ARERunner, *, poll_seconds: float = 0.02, max_turns: int | None = None,
    ) -> None:
        if runner is None or not callable(getattr(runner, "run_turn", None)):
            raise TypeError("an explicit ARE runner port is required")
        _finite_seconds("poll_seconds", poll_seconds)
        if max_turns is not None and (type(max_turns) is not int or max_turns < 0):
            raise ValueError("max_turns must be a non-negative integer or None")
        self._max_turns = max_turns
        self._runner = runner
        self._poll_seconds = poll_seconds
        self._stopped = threading.Event()
        self._completed = threading.Event()
        self._started = False
        self._worker: threading.Thread | None = None

    def run_scenario(
        self,
        scenario: Scenario,
        notification_system: BaseNotificationSystem | None,
        initial_agent_logs: list[BaseAgentLog] | None = None,
    ) -> AgentExecutionResult:
        if self._started:
            raise RuntimeError("ARE agent instance is single-use")
        if not isinstance(scenario, Scenario):
            raise TypeError("an official ARE Scenario is required")
        if notification_system is None or notification_system.time_manager is None:
            raise ValueError("an initialized ARE notification system is required")
        if initial_agent_logs:
            raise NotImplementedError("ARE log replay is outside this bounded bridge")
        self._started = True
        try:
            return self._execute_scenario(scenario, notification_system)
        finally:
            try:
                close = getattr(self._runner, "close", None)
                if close is not None:
                    close()
            finally:
                self._completed.set()

    def _execute_scenario(
        self, scenario: Scenario, notification_system: BaseNotificationSystem
    ) -> AgentExecutionResult:
        start_time = datetime.fromtimestamp(scenario.start_time or 0, tz=UTC)
        # One consumer owns the official queue, even during a blocking run_turn.
        # The runner receives only projected values and a draining inbox port.
        lock = threading.Lock()
        inbox: list[ARENotification] = []
        finished = threading.Event()
        environment_stop = threading.Event()
        failures: list[BaseException] = []

        def state() -> AREVisibleState:
            return AREVisibleState(
                datetime.fromtimestamp(notification_system.get_current_time(), tz=UTC),
                start_time, scenario.additional_system_prompt,
            )

        def collect() -> None:
            messages = notification_system.message_queue.get_by_timestamp(state().simulated_time)
            if any(msg.message_type == MessageType.ENVIRONMENT_STOP for msg in messages):
                for msg in messages:
                    if msg.message_type == MessageType.ENVIRONMENT_NOTIFICATION:
                        notification_system.message_queue.put(msg)
                environment_stop.set()
                self._stopped.set()
                return
            with lock:
                inbox.extend(
                    ARENotification(msg.message_type.value, msg.message, msg.timestamp)
                    for msg in messages
                    if msg.message_type in (
                        MessageType.USER_MESSAGE, MessageType.ENVIRONMENT_NOTIFICATION,
                    )
                )

        def poll() -> tuple[ARENotification, ...]:
            with lock:
                result = tuple(inbox)
                inbox.clear()
                return result

        def watch() -> None:
            try:
                while not finished.wait(self._poll_seconds) and not self._stopped.is_set():
                    collect()
            except BaseException as error:
                failures.append(error)
                self._stopped.set()

        tools = AREToolPort(scenario.get_tools(), self._stopped, poll, state)
        limits = [v for v in (scenario.nb_turns, self._max_turns) if v is not None]
        max_turns = min(limits) if limits else None
        turns = 0
        output: str | None = None
        reason = "stopped"
        if max_turns is not None and max_turns <= 0:
            return AgentExecutionResult(metadata={"terminal_reason": "max_turns", "turns": 0})
        collect()
        monitor = threading.Thread(target=watch, name="ARENotificationMonitor", daemon=True)
        monitor.start()
        try:
            while not self._stopped.is_set():
                visible = poll()
                if not visible:
                    self._stopped.wait(self._poll_seconds)
                    continue
                output = self._runner.run_turn(
                    notifications=visible, state=state(), tools=tools, stop_event=self._stopped,
                )
                if not isinstance(output, (str, type(None))):
                    raise TypeError("ARE runner must return text or None")
                if self._stopped.is_set():
                    output = None
                    break
                turns += 1
                if max_turns is not None and turns >= max_turns:
                    reason = "max_turns"
                    break
        finally:
            finished.set()
            monitor.join()
        if failures:
            raise failures[0]
        if environment_stop.is_set():
            reason = "environment_stop"
            output = None
        metadata = {"terminal_reason": reason, "turns": turns}
        export = getattr(self._runner, "execution_metadata", None)
        if export is not None:
            metadata["sdk"] = export()
        return AgentExecutionResult(output=output, metadata=metadata)

    def stop(self) -> None:
        self._stopped.set()

    def join(self, timeout: float) -> bool:
        """Wait for the physical runner; False means no result is safe to score."""
        _finite_seconds("timeout", timeout, allow_zero=True)
        if self._worker is not None:
            if self._worker is threading.current_thread():
                return False
            self._worker.join(timeout)
            return not self._worker.is_alive()
        if not self._started:
            return True
        return self._completed.wait(timeout)

    async def arun_scenario(
        self,
        scenario: Scenario,
        notification_system: BaseNotificationSystem,
        *,
        join_timeout: float = 1.0,
    ) -> AgentExecutionResult:
        """Run the synchronous ARE contract off-loop and join on cancellation."""
        if self._worker is not None or self._started:
            raise RuntimeError("ARE agent instance is single-use")
        _finite_seconds("join_timeout", join_timeout)
        done = threading.Event()
        result: list[AgentExecutionResult] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                result.append(self.run_scenario(scenario, notification_system))
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        self._worker = threading.Thread(target=work, name="AREBridgeWorker", daemon=True)
        self._worker.start()
        try:
            while not done.is_set():
                await asyncio.sleep(self._poll_seconds)
        except asyncio.CancelledError:
            self.stop()
            if not await asyncio.to_thread(self.join, join_timeout):
                raise AREWorkerStillRunning("ARE runner did not stop within join_timeout") from None
            raise
        if not self.join(join_timeout):
            raise AREWorkerStillRunning("ARE worker did not exit after completion")
        if errors:
            raise errors[0]
        return result[0]

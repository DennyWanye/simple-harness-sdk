# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""``BaseAgent``: the lightweight handle over one durable BaseAgent identity."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from simple_harness.contracts import (
    JsonValue,
    Message,
    MessageRole,
    RunId,
    canonical_json,
    thaw_json,
)
from simple_harness.execution.base_agent import AgentBindingRecord
from simple_harness.execution.sqlite.base_agent.turns import (
    AgentClosedError,
    PendingInputsExhausted,
)
from simple_harness.execution.uow import RunState, UnitOfWorkConflict

from .config import AgentConfig
from .contracts import (
    AgentClosed,
    AgentInputConflict,
    AgentPendingInputsExhausted,
    AgentTurnNotFound,
    AgentTurnReceipt,
    AgentTurnResult,
    AgentTurnState,
    AgentTurnTimeout,
)

if TYPE_CHECKING:
    from .runtime import AgentRuntime


def input_hash_for(message: Message) -> str:
    return hashlib.sha256(
        canonical_json({"message": message.to_dict()}).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentStatus:
    agent_id: str
    run_id: str
    run_state: RunState
    open_turn_id: str | None
    open_turn_phase: str | None
    committed_turns: int

    @property
    def lifecycle(self) -> str:
        """IDLE / RUNNING / FAILED projection (BA-v1.0 §1.3); no separate truth table."""

        if self.run_state in {RunState.FAILED, RunState.CANCELLED}:
            return "FAILED"
        if self.open_turn_id is not None:
            return "RUNNING"
        return "IDLE"


class BaseAgent:
    """Addressable, configurable, recoverable logical Agent (BA-v1.0 §1.1)."""

    def __init__(self, runtime: AgentRuntime, binding: AgentBindingRecord) -> None:
        self._runtime = runtime
        self._binding = binding
        self._config = AgentConfig.from_json(thaw_json(binding.config_json))

    @property
    def agent_id(self) -> str:
        return self._binding.agent_id

    @property
    def run_id(self) -> str:
        return self._binding.run_id

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def role(self) -> str:
        return self._binding.role

    def turn_id_for(self, input_id: str) -> str:
        return f"{self.agent_id}:input:{input_id}"

    async def submit(self, value: str | Message, *, input_id: str) -> AgentTurnReceipt:
        """Durably accept one input; identical replays return the stored receipt."""

        if not isinstance(input_id, str) or not input_id.strip():
            raise ValueError("input_id is required")
        message = Message(MessageRole.USER, value) if isinstance(value, str) else value
        if not isinstance(message, Message):
            raise TypeError("input must be text or a Message")
        payload = message.to_dict()
        try:
            record = await self._runtime.kernel.signal_base_agent_input(
                RunId(self.run_id),
                agent_id=self.agent_id,
                turn_id=self.turn_id_for(input_id),
                input_id=input_id,
                input_hash=input_hash_for(message),
                input_json={"message": payload},
                message=payload,
                max_pending_inputs=self._config.limits.max_pending_inputs,
            )
        except AgentClosedError as error:
            raise AgentClosed(str(error)) from error
        except PendingInputsExhausted as error:
            raise AgentPendingInputsExhausted(str(error)) from error
        except UnitOfWorkConflict as error:
            raise AgentInputConflict(str(error)) from error
        return AgentTurnReceipt(
            turn_id=record.turn_id,
            agent_id=record.agent_id,
            input_id=record.input_id,
            seq=record.seq,
            state=AgentTurnState(record.phase),
        )

    def get_result(self, turn_id: str) -> AgentTurnResult | None:
        """Committed result, or ``None`` while the turn is still open."""

        uow = self._runtime.uow
        stored = uow.read_agent_turn_result(turn_id)
        if stored is not None:
            if stored.agent_id != self.agent_id:
                raise AgentTurnNotFound(turn_id)
            return AgentTurnResult.from_json(stored.result_json)
        turn = uow.read_agent_turn(turn_id)
        if turn is None or turn.agent_id != self.agent_id:
            raise AgentTurnNotFound(turn_id)
        return None

    def turn_state(self, turn_id: str) -> AgentTurnState:
        turn = self._runtime.uow.read_agent_turn(turn_id)
        if turn is None or turn.agent_id != self.agent_id:
            raise AgentTurnNotFound(turn_id)
        return AgentTurnState(turn.phase)

    async def wait_turn(self, turn_id: str, *, timeout: float | None = None) -> AgentTurnResult:
        """Wait for the durable result; a timeout never cancels the underlying turn."""

        clock = self._runtime.ports.clock
        deadline = None if timeout is None else clock() + timeout
        interval = 0.01
        while True:
            result = self.get_result(turn_id)
            if result is not None:
                return result
            if deadline is not None and clock() >= deadline:
                raise AgentTurnTimeout(turn_id)
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.2)

    async def ask(
        self, value: str | Message, *, input_id: str, timeout: float | None = None
    ) -> AgentTurnResult:
        receipt = await self.submit(value, input_id=input_id)
        return await self.wait_turn(receipt.turn_id, timeout=timeout)

    def status(self) -> AgentStatus:
        uow = self._runtime.uow
        run = uow.read_run(self.run_id)
        if run is None:
            raise AgentTurnNotFound(self.run_id)
        open_turn = uow.read_open_agent_turn(self.run_id)
        committed = sum(
            1
            for turn in uow.list_agent_turns(self.agent_id)
            if turn.phase in {"committed", "failed"}
        )
        return AgentStatus(
            agent_id=self.agent_id,
            run_id=self.run_id,
            run_state=run.state,
            open_turn_id=None if open_turn is None else open_turn.turn_id,
            open_turn_phase=None if open_turn is None else open_turn.phase,
            committed_turns=committed,
        )

    def history(self) -> tuple[Mapping[str, JsonValue], ...]:
        """Committed turn results of this Agent only, oldest first (Slice 1 read model)."""

        results = []
        for turn in self._runtime.uow.list_agent_turns(self.agent_id):
            stored = self._runtime.uow.read_agent_turn_result(turn.turn_id)
            if stored is not None:
                results.append(AgentTurnResult.from_json(stored.result_json).to_json())
        return tuple(results)


__all__ = ("AgentStatus", "BaseAgent", "input_hash_for")

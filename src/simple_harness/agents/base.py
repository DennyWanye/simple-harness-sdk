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
    AgentCancelReceipt,
    AgentClosed,
    AgentClosingReceipt,
    AgentInputConflict,
    AgentPendingInputsExhausted,
    AgentTurnNotFound,
    AgentTurnReceipt,
    AgentTurnResult,
    AgentTurnSnapshot,
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
    lifecycle_state: str = "open"
    control_generation: int = 0

    @property
    def lifecycle(self) -> str:
        """IDLE / RUNNING / CLOSING / CLOSED / FAILED projection (BA-v1.0 §1.3).

        Execution state wins over the persisted control intent: a FAILED Run is
        FAILED whatever the binding says; an open turn is RUNNING even while
        closing; otherwise the binding's ``closing`` / ``closed`` intent shows.
        """

        if self.run_state in {RunState.FAILED, RunState.CANCELLED}:
            return "FAILED"
        if self.open_turn_id is not None:
            return "RUNNING"
        if self.lifecycle_state == "closed":
            return "CLOSED"
        if self.lifecycle_state == "closing":
            return "CLOSING"
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

    def turn_snapshot(self, turn_id: str) -> AgentTurnSnapshot:
        """Durable view of one turn: phase, ordinals and whether the Run is blocked on it."""

        uow = self._runtime.uow
        turn = uow.read_agent_turn(turn_id)
        if turn is None or turn.agent_id != self.agent_id:
            raise AgentTurnNotFound(turn_id)
        blocker: dict[str, JsonValue] | None = None
        if turn.phase in {"queued", "running", "result_pending"}:
            open_blockers = uow.list_open_wait_blockers_for_run(self.run_id)
            if open_blockers:
                first = open_blockers[0]
                blocker = {
                    "kind": first.kind.value,
                    "blocker_id": first.blocker_id,
                    "ledger_identity": first.ledger_identity,
                    "handoff_attempt": first.handoff_attempt,
                }
        return AgentTurnSnapshot(
            turn_id=turn.turn_id,
            agent_id=turn.agent_id,
            input_id=turn.input_id,
            seq=turn.seq,
            state=AgentTurnState(turn.phase),
            blocked=blocker is not None,
            blocker=blocker,
            provider_turn_ordinal_from=turn.provider_turn_ordinal_from,
            provider_turn_ordinal_to=turn.provider_turn_ordinal_to,
            created_at=turn.created_at,
        )

    def receipt_for(self, turn_id: str) -> AgentTurnReceipt:
        turn = self._runtime.uow.read_agent_turn(turn_id)
        if turn is None or turn.agent_id != self.agent_id:
            raise AgentTurnNotFound(turn_id)
        return AgentTurnReceipt(
            turn_id=turn.turn_id,
            agent_id=turn.agent_id,
            input_id=turn.input_id,
            seq=turn.seq,
            state=AgentTurnState(turn.phase),
        )

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
                raise AgentTurnTimeout(turn_id, self.receipt_for(turn_id))
            await asyncio.sleep(interval)
            interval = min(interval * 2, 0.2)

    async def ask(
        self, value: str | Message, *, input_id: str, timeout: float | None = None
    ) -> AgentTurnResult:
        receipt = await self.submit(value, input_id=input_id)
        return await self.wait_turn(receipt.turn_id, timeout=timeout)

    async def close(self, *, command_id: str, drain_timeout: float = 30.0) -> AgentClosingReceipt:
        """Refuse new inputs durably (BA12); the Run stays alive and history readable."""

        receipt = await self._runtime.close_agent(
            self.agent_id, command_id=command_id, drain_timeout=drain_timeout
        )
        refreshed = self._runtime.uow.read_agent_binding(self.agent_id)
        if refreshed is not None:
            self._binding = refreshed
        return receipt

    async def cancel_turn(
        self, turn_id: str, *, command_id: str, wait_timeout: float = 30.0
    ) -> AgentCancelReceipt:
        """Cooperatively cancel one open turn; committed facts are never rewritten."""

        turn = self._runtime.uow.read_agent_turn(turn_id)
        if turn is None or turn.agent_id != self.agent_id:
            raise AgentTurnNotFound(turn_id)
        receipt = await self._runtime.cancel_turn(
            self.agent_id, turn_id, command_id=command_id, wait_timeout=wait_timeout
        )
        refreshed = self._runtime.uow.read_agent_binding(self.agent_id)
        if refreshed is not None:
            self._binding = refreshed
        return receipt

    def status(self) -> AgentStatus:
        uow = self._runtime.uow
        run = uow.read_run(self.run_id)
        if run is None:
            raise AgentTurnNotFound(self.run_id)
        binding = uow.read_agent_binding(self.agent_id) or self._binding
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
            lifecycle_state=binding.lifecycle,
            control_generation=binding.control_generation,
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

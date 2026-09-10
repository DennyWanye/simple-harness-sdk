# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Bridge to the finished BaseAgent runtime (ORCH §2, §4.3 steps 2–4, D4/D5'/D6').

One ``AgentRuntime`` per orchestrator process.  Every dispatch intent freezes the
complete ``AgentConfig`` JSON and the complete input ``Message`` JSON; the bridge
replays those bytes and never rebuilds them.  ``creation_key`` / ``input_id`` are
the SDK-side idempotency keys, so a replay after a crash lands on the very same
Agent and Turn (S2-04).  Liveness is read from ``turn_snapshot`` — the bridge
never invents a heartbeat (§12.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from simple_harness import Message, MessageRole, RunId
from simple_harness.agents import AgentConfig, AgentTurnResult, AgentTurnState
from simple_harness.agents.contracts import _message_from_json
from simple_harness.agents.runtime import AgentRuntime

from ..governance.budgets import UsageFact

ALIVE_STATES = {AgentTurnState.QUEUED, AgentTurnState.RUNNING, AgentTurnState.RESULT_PENDING}


@dataclass(frozen=True, slots=True)
class Liveness:
    """What the executor's own records say about a turn (D6')."""

    exists: bool
    state: str | None
    blocked: bool
    blocker: Mapping[str, Any] | None
    progress: int | None  # provider_turn_ordinal_to
    settled: bool

    @property
    def alive(self) -> bool:
        return self.exists and not self.settled

    def to_json(self) -> dict[str, Any]:
        return {
            "exists": self.exists,
            "state": self.state,
            "blocked": self.blocked,
            "blocker": None if self.blocker is None else dict(self.blocker),
            "progress": self.progress,
            "settled": self.settled,
        }


class AgentBridge:
    def __init__(self, runtime: AgentRuntime, *, unpriced: bool) -> None:
        self._runtime = runtime
        self._unpriced = unpriced

    @property
    def runtime(self) -> AgentRuntime:
        return self._runtime

    @property
    def unpriced(self) -> bool:
        return self._unpriced

    def check_tools(self, config: AgentConfig) -> None:
        missing = set(config.tool_names) - set(self._runtime.tool_names)
        if missing:
            raise ValueError(f"tools not registered in the runtime: {sorted(missing)}")

    async def create(
        self, *, creation_key: str, config_json: Mapping[str, Any]
    ) -> tuple[str, str, str]:
        """Idempotent create from frozen config bytes → (agent_id, run_id, "")."""

        config = AgentConfig.from_json(dict(config_json))
        self.check_tools(config)
        agent = await self._runtime.create(config, creation_key=creation_key)
        return agent.agent_id, agent.run_id, ""

    async def submit(
        self, *, agent_id: str, input_id: str, message_json: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        agent = await self._runtime.open(agent_id)
        message = _message_from_json(dict(message_json))
        receipt = await agent.submit(message, input_id=input_id)
        return {
            "turn_id": receipt.turn_id,
            "seq": receipt.seq,
            "state": str(receipt.state),
            "agent_id": receipt.agent_id,
        }

    async def expected_turn_id(self, *, agent_id: str, input_id: str) -> str:
        agent = await self._runtime.open(agent_id)
        return agent.turn_id_for(input_id)

    async def result(self, *, agent_id: str, turn_id: str) -> AgentTurnResult | None:
        agent = await self._runtime.open(agent_id)
        return agent.get_result(turn_id)

    async def liveness(self, *, agent_id: str, turn_id: str) -> Liveness:
        try:
            agent = await self._runtime.open(agent_id)
        except Exception:  # noqa: BLE001 - unknown agent == not alive
            return Liveness(False, None, False, None, None, False)
        try:
            snapshot = agent.turn_snapshot(turn_id)
        except Exception:  # noqa: BLE001 - unknown turn
            return Liveness(False, None, False, None, None, False)
        state = AgentTurnState(snapshot.state)
        return Liveness(
            exists=True,
            state=str(state),
            blocked=bool(snapshot.blocked),
            blocker=None if snapshot.blocker is None else dict(snapshot.blocker),
            progress=snapshot.provider_turn_ordinal_to,
            settled=state in {AgentTurnState.COMMITTED, AgentTurnState.FAILED},
        )

    def usage_facts(self, *, agent_id: str) -> list[UsageFact]:
        """Cost facts from the SDK invocation ledger (D10'): one fact per invocation."""

        facts = []
        for record in self._runtime.uow.list_provider_invocations(RunId(agent_id)):
            usage = record.usage_json if isinstance(record.usage_json, Mapping) else {}
            tokens = usage.get("usage") if isinstance(usage, Mapping) else None
            input_tokens = int((tokens or {}).get("input_tokens") or 0)
            output_tokens = int((tokens or {}).get("output_tokens") or 0)
            charge = record.budget_charge
            amount = None if self._unpriced else charge.amount_micros
            facts.append(
                UsageFact(
                    f"provider-invocation:{record.invocation_id}",
                    input_tokens,
                    output_tokens,
                    amount,
                )
            )
        return facts

    def has_unknown_charge(self, *, agent_id: str) -> bool:
        return bool(self._runtime.uow.read_provider_budget(RunId(agent_id)).has_unknown_charge)

    async def recover(self) -> None:
        await self._runtime.recover_pending_turns()


def user_message_json(text: str) -> dict[str, Any]:
    return Message(MessageRole.USER, text).to_dict()


__all__ = ("ALIVE_STATES", "AgentBridge", "Liveness", "user_message_json")

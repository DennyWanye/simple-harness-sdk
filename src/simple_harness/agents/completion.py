# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Single place that turns a ReAct loop result (or failure) into an AgentTurnOutcome."""

from __future__ import annotations

from collections.abc import Mapping

from simple_harness.contracts import JsonValue, Message
from simple_harness.runtime.agent_turn import AgentTurnOutcome

from .contracts import AgentTurnResult, AgentTurnState


def committed_outcome(
    *,
    agent_id: str,
    turn_id: str,
    seq: int,
    input_id: str,
    input_hash: str,
    response_message: Message,
    usage_refs: tuple[str, ...] = (),
    delegation_count: int = 0,
    provider_turn_ordinal_from: int | None = None,
    provider_turn_ordinal_to: int | None = None,
) -> AgentTurnOutcome:
    result = AgentTurnResult(
        turn_id=turn_id,
        agent_id=agent_id,
        seq=seq,
        state=AgentTurnState.COMMITTED,
        public_output=response_message,
        usage_refs=usage_refs,
        delegation_count=delegation_count,
    )
    return result.to_outcome(
        input_id=input_id,
        input_hash=input_hash,
        provider_turn_ordinal_from=provider_turn_ordinal_from,
        provider_turn_ordinal_to=provider_turn_ordinal_to,
    )


def failed_outcome(
    *,
    agent_id: str,
    turn_id: str,
    seq: int,
    input_id: str,
    input_hash: str,
    error: Mapping[str, JsonValue],
    delegation_count: int = 0,
    provider_turn_ordinal_from: int | None = None,
    provider_turn_ordinal_to: int | None = None,
) -> AgentTurnOutcome:
    """A turn that failed on its own terms; the Agent itself stays alive."""

    result = AgentTurnResult(
        turn_id=turn_id,
        agent_id=agent_id,
        seq=seq,
        state=AgentTurnState.FAILED,
        error=dict(error),
        delegation_count=delegation_count,
    )
    return result.to_outcome(
        input_id=input_id,
        input_hash=input_hash,
        provider_turn_ordinal_from=provider_turn_ordinal_from,
        provider_turn_ordinal_to=provider_turn_ordinal_to,
    )


__all__ = ("committed_outcome", "failed_outcome")

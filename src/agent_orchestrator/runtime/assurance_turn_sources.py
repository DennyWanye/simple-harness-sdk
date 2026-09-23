# SPDX-License-Identifier: Apache-2.0
"""Read actual BaseAgent/Provider inputs from the deployed original execution UoW."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from typing import TYPE_CHECKING, Any

from simple_harness.agents import AgentTurnResult, AgentTurnState
from simple_harness.contracts import RunId, canonical_json, thaw_json
from simple_harness.execution.provider_invocations import (
    provider_invocation_id,
    provider_request_fingerprint,
    provider_request_from_json,
    provider_request_json,
    provider_response_from_json,
)

from ..assurance.codec import AssuranceError, canonical, fingerprint

if TYPE_CHECKING:
    from ..storage.store import DispatchIntent
    from .agent_worker import AgentBridge


@dataclass(frozen=True, slots=True)
class ActualReviewTurn:
    result_json: str
    source_json: str
    # Missing exposure proof never prevents retaining actual raw output/accounting.
    exposure_json: str | None
    exposure_error: str | None


def read_actual_review_turn(bridge: AgentBridge, intent: DispatchIntent) -> ActualReviewTurn:
    """No caller-supplied result or receipt. Read the runtime's persisted objects.

    The immutable turn result is retained even when the Provider request/context
    manifest is unavailable; such a turn cannot authorize an official review.
    """
    if not intent.agent_id or not intent.expected_turn_id:
        raise AssuranceError("REVIEW_TURN_UNAVAILABLE")
    uow = bridge.runtime.uow
    with uow.database.transaction():
        agent = uow.read_agent_binding(intent.agent_id)
        turn = uow.read_agent_turn(intent.expected_turn_id)
        stored = uow.read_agent_turn_result(intent.expected_turn_id)
        if agent is None or turn is None or stored is None:
            raise AssuranceError("REVIEW_TURN_UNAVAILABLE")
        raw = thaw_json(stored.result_json)
        raw_json = canonical_json(raw)
        result = AgentTurnResult.from_json(raw)
        expected_input = {"message": dict(intent.config["message"])}
        if (
            agent.creation_key != intent.creation_key
            or thaw_json(agent.config_json) != dict(intent.config["agent_config"])
            or turn.agent_id != intent.agent_id
            or stored.agent_id != intent.agent_id
            or result.agent_id != intent.agent_id
            or result.turn_id != intent.expected_turn_id
            or stored.turn_id != intent.expected_turn_id
            or turn.input_id != intent.input_id
            or result.seq != turn.seq
            or hashlib.sha256(raw_json.encode()).hexdigest() != stored.result_hash
            or thaw_json(turn.input_json) != expected_input
            or turn.input_hash != fingerprint(expected_input)
            or intent.input_hash != fingerprint(expected_input["message"])
            or turn.phase != result.state.value
        ):
            raise AssuranceError("REVIEW_RUNTIME_SOURCE_MISMATCH")
        source = {
            "agent_id": result.agent_id,
            "run_id": agent.run_id,
            "turn_id": result.turn_id,
            "input_id": turn.input_id,
            "agent_input_hash": turn.input_hash,
            "intent_input_hash": intent.input_hash,
            "agent_config_hash": agent.config_hash,
            "result_hash": stored.result_hash,
            "commit_receipt_id": stored.commit_receipt_id,
            "committed_at": stored.committed_at,
            "state": result.state.name,
            "usage_refs": list(stored.usage_refs),
        }
        try:
            exposure = _exposure(uow, agent.run_id, turn, result)
        except AssuranceError as error:
            return ActualReviewTurn(raw_json, canonical(source), None, error.code)
        return ActualReviewTurn(raw_json, canonical(source), canonical(exposure), None)


def _exposure(uow: Any, run_id: str, turn: Any, result: AgentTurnResult) -> dict:
    from simple_harness.contracts import RequestId

    if result.state is not AgentTurnState.COMMITTED:
        raise AssuranceError("REVIEW_TURN_NOT_COMMITTED")
    # AgentTurnOutcome names the final Provider response; earlier tool turns do
    # not prove what this final response actually saw.
    request_ids = [
        ref.removeprefix("provider-request:")
        for ref in result.usage_refs
        if ref.startswith("provider-request:")
    ]
    if len(request_ids) != 1:
        raise AssuranceError("REVIEW_PROVIDER_SOURCE_UNAVAILABLE")
    request_id = request_ids[0]
    provider = uow.read_provider_invocation(
        provider_invocation_id(RunId(run_id), RequestId(request_id))
    )
    selection = uow.read_agent_context_selection_by_request(request_id)
    if provider is None or selection is None or provider.request_json is None:
        raise AssuranceError("REVIEW_PROVIDER_INPUT_UNAVAILABLE")
    request = thaw_json(provider.request_json)
    response = thaw_json(provider.response_json) if provider.response_json is not None else None
    try:
        decoded_request = provider_request_from_json(RequestId(request_id), request)
        decoded_response = provider_response_from_json(response)
    except (TypeError, ValueError, KeyError) as error:
        raise AssuranceError("REVIEW_PROVIDER_INPUT_MISMATCH") from error
    if (
        provider.state.value != "succeeded"
        or provider.handed_off_at is None
        or provider.settled_at is None
        or fingerprint(request) != provider.request_fingerprint
        or selection.agent_id != result.agent_id
        or selection.turn_id != result.turn_id
        or not _selection_binds_request(uow, run_id, selection, provider, decoded_request)
        or response is None
        or response.get("request_id") != request_id
        or result.public_output is None
        or decoded_response.message.to_dict() != result.public_output.to_dict()
        or provider_request_json(decoded_request) != request
    ):
        raise AssuranceError("REVIEW_PROVIDER_INPUT_MISMATCH")
    # Provider storage writes explicit null name/call_id fields; the Agent
    # journal uses Message.to_dict(), which omits absent optional fields. Round
    # trip through the original Provider codec before comparing Message bytes.
    messages = [message.to_dict() for message in decoded_request.messages]
    if selection.source_highwater > 20_000 or len(selection.selected_seqs) > 20_000:
        raise AssuranceError("EVIDENCE_EVALUATION_INCOMPLETE")
    # Read to the original frozen highwater. A later journal append cannot be
    # passed off as having preceded this request.
    journal = uow.read_agent_journal(result.agent_id, from_seq=1, to_seq=selection.source_highwater)
    by_seq = {item.seq: item for item in journal}
    selected = []
    for seq in selection.selected_seqs:
        item = by_seq.get(seq)
        if item is None:
            raise AssuranceError("REVIEW_JOURNAL_INCOMPLETE")
        message = thaw_json(item.message_json)
        if fingerprint(message) != item.content_hash:
            raise AssuranceError("REVIEW_JOURNAL_HASH_MISMATCH")
        # Context compaction/transforms can alter a message. Only exact bytes in
        # the actual final request count as exposure of the original material.
        if message in messages:
            selected.append(
                {
                    "message_id": item.record_id,
                    "seq": seq,
                    "kind": item.kind,
                    "turn_id": item.turn_id,
                    "message": message,
                    "message_hash": item.content_hash,
                }
            )
    return {
        "provider_invocation_id": provider.invocation_id,
        "provider_request_id": request_id,
        "provider_input_hash": provider.request_fingerprint,
        # Fingerprint of the wire copy the Provider physically received (§4); it
        # differs from the persisted composed request only on tool turns.
        "wire_input_hash": selection.request_hash,
        "provider_request": request,
        "provider_response_hash": fingerprint(response),
        "response_model": decoded_response.model,
        "selection_id": selection.selection_id,
        "source_highwater": selection.source_highwater,
        "selected_message_ids": [item["message_id"] for item in selected],
        "messages": selected,
    }


def _selection_binds_request(
    uow: Any, run_id: str, selection: Any, provider: Any, request: Any
) -> bool:
    """The frozen selection must name this exact persisted request.

    The runtime persists the composed request and binds the selection to the wire
    copy it actually sent. On a tool turn the wire copy rewrites assistant
    messages with their ledger tool calls (tool/user message bytes are unchanged),
    so the persisted composed bytes are re-rendered through the same original
    wire rule and the durable effect ledger before comparing. No new bytes are
    invented; a selection that matches neither is not this request's manifest.
    """
    if selection.request_hash == provider.request_fingerprint:
        return True
    from simple_harness.agents.wire import _ledger_groups, restore_tool_calls

    if not any(message.role.value == "tool" for message in request.messages):
        return False
    wire_messages, _ = restore_tool_calls(
        request.messages, _ledger_groups(uow.database.connection, run_id)
    )
    return selection.request_hash == provider_request_fingerprint(
        replace(request, messages=wire_messages)
    )

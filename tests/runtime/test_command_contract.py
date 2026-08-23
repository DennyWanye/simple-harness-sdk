# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path

import pytest

from simple_harness import (
    COMMAND_FRAME_MAX_BYTES,
    COMMAND_MESSAGE_MAX_BYTES,
    AgentIdentity,
    CancelCommandIntent,
    CommandError,
    CommandErrorCode,
    CommandKind,
    CommandOutputState,
    CommandReceipt,
    CommandRetryState,
    CommandSnapshot,
    CommandState,
    ContinueCommandIntent,
    Message,
    MessageRole,
    RequestId,
    RunApiMode,
    RunId,
    StartCommandIntent,
    canonical_json,
)
from simple_harness.runtime import ConversationContinuationInput, ConversationTurnInput


def _start() -> StartCommandIntent:
    return StartCommandIntent(
        "phone/deployment-1",
        "projection-key-1",
        "command-start-1",
        RunId("run-1"),
        RequestId("request-1"),
        "turn-1",
        ConversationTurnInput(
            AgentIdentity("deployment-1", "household-1", "user-1", "session-1"),
            Message(MessageRole.USER, "hello"),
            "hello",
        ),
    )


def test_command_contract_is_closed_and_matches_frozen_artifact() -> None:
    artifact = json.loads(
        (Path(__file__).parents[1] / "unit/contracts/command-public-api.json").read_text()
    )
    assert artifact["api_mode"] == [member.value for member in RunApiMode]
    assert artifact["command_kind"] == [member.value for member in CommandKind]
    assert artifact["command_state"] == [member.value for member in CommandState]
    assert artifact["retry_state"] == [member.value for member in CommandRetryState]
    assert artifact["output_state"] == [member.value for member in CommandOutputState]
    assert artifact["limits"] == {
        "message_bytes": COMMAND_MESSAGE_MAX_BYTES,
        "frame_bytes": COMMAND_FRAME_MAX_BYTES,
    }
    assert artifact["receipt_fields"] == [field.name for field in fields(CommandReceipt)]
    assert artifact["snapshot_fields"] == [field.name for field in fields(CommandSnapshot)]


def test_command_intents_have_deterministic_canonical_identity() -> None:
    start = _start()
    assert (
        start.intent_hash
        == hashlib.sha256(canonical_json(start.to_json()).encode("utf-8")).hexdigest()
    )
    assert start.intent_hash == "04216265fd682be3a4f0c92e11bcdd478d46dcbb9aa85f3e92f205d7903d5bfa"
    continuation = ContinueCommandIntent(
        start.namespace,
        start.projection_key_id,
        "command-continue-1",
        start.run_id,
        "continuation-1",
        "turn-2",
        ConversationContinuationInput(Message(MessageRole.USER, "next"), "next"),
    )
    cancel = CancelCommandIntent(
        start.namespace, start.projection_key_id, "command-cancel-1", start.run_id
    )
    assert continuation.kind is CommandKind.CONTINUE
    assert cancel.kind is CommandKind.CANCEL
    assert continuation.intent_hash != cancel.intent_hash != start.intent_hash


def test_receipt_and_snapshot_reject_open_or_raw_payload_shapes() -> None:
    start = _start()
    receipt = CommandReceipt(
        start.command_id,
        start.run_id,
        start.kind,
        0,
        CommandState.ACCEPTED,
        1,
        start.namespace,
        start.projection_key_id,
        start.intent_hash,
    )
    snapshot = CommandSnapshot(receipt, CommandRetryState.READY, CommandOutputState.PENDING)
    assert snapshot.output is None
    assert not hasattr(snapshot, "raw_payload")
    with pytest.raises(TypeError):
        CommandReceipt(  # type: ignore[call-arg]
            start.command_id,
            start.run_id,
            start.kind,
            0,
            CommandState.ACCEPTED,
            1,
            start.namespace,
            start.projection_key_id,
            start.intent_hash,
            unknown="value",
        )
    with pytest.raises(ValueError, match="present"):
        CommandSnapshot(receipt, CommandRetryState.SETTLED, CommandOutputState.PRESENT)
    with pytest.raises(ValueError):
        CommandState("unknown")
    assert CommandError(CommandErrorCode.INTENT_CONFLICT).args == ("command_intent_conflict",)


def test_message_limit_fails_closed_before_admission() -> None:
    with pytest.raises(CommandError) as raised:
        StartCommandIntent(
            "namespace",
            "key",
            "command",
            RunId("run"),
            RequestId("request"),
            "turn",
            ConversationTurnInput(
                AgentIdentity("deployment", "household", "actor", "session"),
                Message(MessageRole.USER, "x" * COMMAND_MESSAGE_MAX_BYTES),
                "x" * COMMAND_MESSAGE_MAX_BYTES,
            ),
        )
    assert raised.value.code is CommandErrorCode.PAYLOAD_TOO_LARGE

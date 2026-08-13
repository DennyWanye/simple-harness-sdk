# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from simple_harness import CallId, ContractValidationError, Message, MessageRole


def test_message_is_deeply_immutable_and_serializes_to_plain_json() -> None:
    metadata = {"source": {"kind": "host"}, "refs": ["one"]}
    message = Message(role=MessageRole.USER, content="hello", metadata=metadata)
    metadata["source"]["kind"] = "mutated"
    metadata["refs"].append("two")

    assert message.to_dict() == {
        "role": "user",
        "content": "hello",
        "metadata": {"source": {"kind": "host"}, "refs": ["one"]},
    }
    with pytest.raises(TypeError):
        message.metadata["extra"] = True  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        message.content = "changed"  # type: ignore[misc]


def test_tool_message_requires_typed_call_id() -> None:
    message = Message(
        role="tool",
        content="done",
        name="project.read",
        call_id=CallId("call-1"),
    )
    assert message.role is MessageRole.TOOL
    assert message.to_dict()["call_id"] == "call-1"

    with pytest.raises(ContractValidationError, match="tool message requires call_id"):
        Message(role="tool", content="done")


def test_non_tool_message_cannot_impersonate_tool_result() -> None:
    with pytest.raises(ContractValidationError, match="only tool messages"):
        Message(role="assistant", content="done", call_id=CallId("call-1"))


@pytest.mark.parametrize("role", ["invalid", "", 7])
def test_unknown_roles_are_stable_contract_errors(role: object) -> None:
    with pytest.raises(ContractValidationError) as error:
        Message(role=role, content="hello")  # type: ignore[arg-type]
    assert error.value.code == "invalid_message"


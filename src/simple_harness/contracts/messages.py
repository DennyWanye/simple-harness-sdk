# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Immutable model/host message contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias, cast

from .errors import ContractValidationError, ErrorCode
from .identity import CallId
from .json import FrozenJsonValue, JsonValue, freeze_json, thaw_json


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ContentBlock:
    """Provider-neutral structured message content block.

    ``data`` contains every field except the discriminator.  Keeping the
    discriminator typed prevents callers from accidentally passing a list and
    relying on its string representation at a transport boundary.
    """

    type: str
    data: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip():
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "content block type must be non-empty"
            )
        if not isinstance(self.data, Mapping) or "type" in self.data:
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE,
                "content block data must be an object without a type field",
            )
        frozen = freeze_json(dict(self.data))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "type", self.type.strip())
        object.__setattr__(self, "data", frozen)

    def to_dict(self) -> dict[str, JsonValue]:
        data = thaw_json(cast(FrozenJsonValue, self.data))
        assert isinstance(data, dict)
        return {"type": self.type, **data}

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> ContentBlock:
        block_type = value.get("type")
        if not isinstance(block_type, str):
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "content block type must be a string"
            )
        return cls(block_type, {key: item for key, item in value.items() if key != "type"})


MessageContent: TypeAlias = str | tuple[ContentBlock, ...]


@dataclass(frozen=True, slots=True)
class Message:
    role: MessageRole | str
    content: MessageContent
    name: str | None = None
    call_id: CallId | None = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            role = MessageRole(self.role)
        except (TypeError, ValueError) as error:
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "message role is not supported"
            ) from error
        if not isinstance(self.content, (str, tuple)):
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE,
                "message content must be text or a tuple of ContentBlock values",
            )
        if isinstance(self.content, tuple) and not all(
            isinstance(block, ContentBlock) for block in self.content
        ):
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE,
                "structured message content must contain ContentBlock values",
            )
        if self.name is not None and (not isinstance(self.name, str) or not self.name.strip()):
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "message name must be non-empty"
            )
        if self.call_id is not None and not isinstance(self.call_id, CallId):
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "message call_id must use CallId"
            )
        if role is MessageRole.TOOL and self.call_id is None:
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "tool message requires call_id"
            )
        if role is not MessageRole.TOOL and self.call_id is not None:
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "only tool messages may carry call_id"
            )
        if not isinstance(self.metadata, dict):
            raise ContractValidationError(
                ErrorCode.INVALID_MESSAGE, "message metadata must be a JSON object"
            )
        frozen = freeze_json(self.metadata)
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "name", self.name.strip() if self.name else None)
        object.__setattr__(self, "metadata", frozen)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "role": self.role.value,
            "content": (
                self.content
                if isinstance(self.content, str)
                else [block.to_dict() for block in self.content]
            ),
            "metadata": thaw_json(self.metadata),  # type: ignore[arg-type]
        }
        if self.name is not None:
            result["name"] = self.name
        if self.call_id is not None:
            result["call_id"] = self.call_id.value
        return result


__all__ = ("ContentBlock", "Message", "MessageContent", "MessageRole")

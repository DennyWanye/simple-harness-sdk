# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Stable, minimally disclosed error contracts."""

from __future__ import annotations

from enum import StrEnum
import re
from types import TracebackType
from typing import Any


_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ErrorCode(StrEnum):
    """Foundation error codes shared by public SDK boundaries."""

    INVALID_REQUEST = "invalid_request"
    INVALID_JSON = "invalid_json"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_MESSAGE = "invalid_message"
    INVALID_EVENT = "invalid_event"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TOOL_REJECTED = "tool_rejected"
    UNKNOWN_OUTCOME = "unknown_outcome"
    INTERNAL_ERROR = "internal_error"


class HarnessError(RuntimeError):
    """A stable public failure that never renders its private cause."""

    __slots__ = ("code", "public_message", "retryable", "_private_cause")

    def __init__(
        self,
        code: ErrorCode | str,
        public_message: str,
        *,
        retryable: bool = False,
        private_cause: BaseException | None = None,
    ) -> None:
        code_value = code.value if isinstance(code, ErrorCode) else code
        if not isinstance(code_value, str) or not _ERROR_CODE.fullmatch(code_value):
            raise ValueError("error code must be lowercase snake_case and at most 64 chars")
        if not isinstance(public_message, str) or not public_message.strip():
            raise ValueError("public_message must be a non-empty string")
        if not isinstance(retryable, bool):
            raise TypeError("retryable must be a bool")
        if private_cause is not None and not isinstance(private_cause, BaseException):
            raise TypeError("private_cause must be an exception")
        super().__init__(public_message.strip())
        self.code = code_value
        self.public_message = public_message.strip()
        self.retryable = retryable
        self._private_cause = private_cause

    def to_dict(self) -> dict[str, bool | int | str]:
        return {
            "schema_version": 1,
            "code": self.code,
            "public_message": self.public_message,
            "retryable": self.retryable,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"public_message={self.public_message!r}, retryable={self.retryable!r})"
        )

    def with_traceback(self, tb: TracebackType | None) -> "HarnessError":
        return super().with_traceback(tb)


class ContractValidationError(HarnessError, ValueError):
    """A public contract value failed deterministic validation."""

    def __init__(self, code: ErrorCode | str, public_message: str) -> None:
        super().__init__(code, public_message, retryable=False)


__all__ = ("ErrorCode", "HarnessError", "ContractValidationError")


# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Boundary redaction helpers for provider diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .base import Secret

REDACTED = "[REDACTED]"


@dataclass(frozen=True, slots=True, repr=False)
class SecretRedactor:
    """Redact injected secret values from strings and JSON-like diagnostics."""

    _values: tuple[str, ...]

    @classmethod
    def from_secrets(cls, *secrets: Secret) -> SecretRedactor:
        values = tuple(
            sorted(
                {secret.reveal() for secret in secrets if secret.reveal()},
                key=len,
                reverse=True,
            )
        )
        return cls(values)

    def text(self, value: object) -> str:
        redacted = str(value)
        for secret in self._values:
            redacted = redacted.replace(secret, REDACTED)
        return redacted

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(key): self.value(item) for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            return [self.value(item) for item in value]
        return value

    def exception(self, exc: BaseException) -> RuntimeError:
        return RuntimeError(self.text(exc))

    def __repr__(self) -> str:
        return "SecretRedactor([REDACTED])"

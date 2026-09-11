# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Credential patterns (§10.2 / §21.3 "密钥不进入模型上下文", S6-09 "无密钥", plan D6-10).

One place for the value patterns every guard uses: the context builder refuses a model
package that carries one, the evidence writer refuses to write a file that contains one.
A finding names the pattern and the location, never the matched value."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key_sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
# credentials a deployment keeps in its environment: checked by value, never printed
SECRET_ENV_NAMES = ("SH_APIKEY", "APIKEY", "DEEPSEEK_APIKEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


class SecretLeak(ValueError):
    """Raised instead of writing / sending text that carries a credential."""


def environment_secrets(names: Iterable[str] = SECRET_ENV_NAMES) -> tuple[str, ...]:
    return tuple(v for n in names if len(v := os.environ.get(n, "")) >= 12)


def find_secrets(text: str, *, extra: Iterable[str] = ()) -> list[str]:
    """Names of the patterns found in ``text`` (``env_value`` for a configured secret)."""

    found = [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]
    if any(value and value in text for value in extra):
        found.append("env_value")
    return found


def guard_text(text: str, *, where: str) -> None:
    found = find_secrets(text, extra=environment_secrets())
    if found:
        raise SecretLeak(f"{where} would carry a credential ({', '.join(found)}); not written")


__all__ = (
    "SECRET_ENV_NAMES",
    "SECRET_PATTERNS",
    "SecretLeak",
    "environment_secrets",
    "find_secrets",
    "guard_text",
)

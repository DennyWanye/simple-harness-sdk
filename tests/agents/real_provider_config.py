# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Resolve a real OpenAI-compatible endpoint for opt-in tests; never prints or stores the key.

Priority: ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` environment variables, then the
Host repository's ``.env`` (``BASEURL`` / ``APIKEY``).  Missing pieces mean *skip*.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "gpt-5.6-luna"
HOST_ENV = Path(__file__).resolve().parents[3] / "simple_harness" / ".env"


@dataclass(frozen=True, slots=True)
class RealProviderConfig:
    base_url: str
    api_key: str
    model: str

    def __repr__(self) -> str:  # never leak the key through reprs/asserts
        return (
            f"RealProviderConfig(base_url={self.base_url!r}, model={self.model!r}, "
            "api_key=<redacted>)"
        )


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_real_provider() -> RealProviderConfig | None:
    env = os.environ
    dotenv = _dotenv(HOST_ENV)
    base_url = env.get("SH_BASEURL") or dotenv.get("BASEURL")
    api_key = env.get("SH_APIKEY") or dotenv.get("APIKEY")
    model = env.get("SH_MODEL") or dotenv.get("MODEL") or DEFAULT_MODEL
    if not base_url or not api_key:
        return None
    return RealProviderConfig(base_url=base_url, api_key=api_key, model=model)


def build_real_provider(config: RealProviderConfig, *, timeout: float = 180.0):  # type: ignore[no-untyped-def]
    import httpx

    from simple_harness.providers import OpenAICompatibleProvider, Secret

    return OpenAICompatibleProvider(
        httpx.AsyncClient(),
        config.base_url,
        config.model,
        Secret(config.api_key),
        timeout=timeout,
    )


__all__ = ("DEFAULT_MODEL", "RealProviderConfig", "build_real_provider", "resolve_real_provider")

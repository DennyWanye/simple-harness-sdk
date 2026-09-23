# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Resolve a real OpenAI-compatible endpoint for opt-in tests; never prints or stores the key.

Priority: ``SH_BASEURL`` / ``SH_APIKEY`` / ``SH_MODEL`` environment variables, then the
V1.4 DeepSeek lane in the Host repository's ``.env`` (``DEEPSEEKER_*``), then the
legacy ``BASEURL`` / ``APIKEY`` fields. Missing pieces mean *skip*.
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
    base_url = env.get("SH_BASEURL") or dotenv.get("DEEPSEEKER_BASEURL") or dotenv.get("BASEURL")
    api_key = env.get("SH_APIKEY") or dotenv.get("DEEPSEEKER_APIKEY") or dotenv.get("APIKEY")
    model = (
        env.get("SH_MODEL")
        or dotenv.get("DEEPSEEKER_MODEL")
        or dotenv.get("MODEL")
        or DEFAULT_MODEL
    )
    if not base_url or not api_key:
        return None
    return RealProviderConfig(base_url=base_url, api_key=api_key, model=model)


def build_real_provider(config: RealProviderConfig, *, timeout: float = 180.0):  # type: ignore[no-untyped-def]
    import httpx

    from simple_harness.providers import OpenAICompatibleProvider, Secret

    # The DeepSeek relay currently terminates TLS 1.2 reliably but intermittently
    # closes TLS 1.3 handshakes with ``SSL_UNEXPECTED_EOF`` on this macOS runner.
    # This is a transport compatibility setting, not a quota or retry workaround;
    # the SDK Provider still owns status classification and durable handoff rules.
    hostname = (urlparse(config.base_url).hostname or "").lower()
    verify: bool | ssl.SSLContext = True
    if hostname.endswith(".cpolar.top"):
        tls12 = ssl.create_default_context()
        tls12.minimum_version = ssl.TLSVersion.TLSv1_2
        tls12.maximum_version = ssl.TLSVersion.TLSv1_2
        verify = tls12
    return OpenAICompatibleProvider(
        httpx.AsyncClient(verify=verify, trust_env=False),
        config.base_url,
        config.model,
        Secret(config.api_key),
        timeout=timeout,
    )


__all__ = ("DEFAULT_MODEL", "RealProviderConfig", "build_real_provider", "resolve_real_provider")

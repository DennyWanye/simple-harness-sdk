# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Shared executable runner used by the CLI and pytest plugin."""

from __future__ import annotations

import inspect
import platform as platform_module
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import cast

from simple_harness.version import __version__

from .contracts import (
    CaseStatus,
    ConformanceCaseUnavailable,
    ConformanceCaseResult,
    ConformanceError,
    ConformanceHost,
    ConformanceHostMetadata,
    ConformanceReport,
    CaseObservation,
)
from .suites import CASES_BY_SUITE
from .verifiers import verify_observation


PROTOCOL_VERSION = "1.0.0"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "provider_body",
    "raw_body",
    "raw_provider",
    "secret",
    "token",
    "api_key",
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+\S+|(?:sk|tsk|key)[-_][A-Za-z0-9._-]{6,})"
)
_MAX_PUBLIC_TEXT = 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_suite_names(suites: tuple[str, ...]) -> tuple[str, ...]:
    if not suites:
        raise ValueError("at least one conformance suite is required")
    normalized = tuple(item.strip() for item in suites)
    if any(not item for item in normalized):
        raise ValueError("conformance suite names cannot be empty")
    invalid = sorted(set(normalized) - set(CASES_BY_SUITE))
    if invalid:
        raise ValueError(f"invalid conformance suites: {invalid}")
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate conformance suite names are forbidden")
    return normalized


def _major(version: str) -> int:
    try:
        return int(version.split(".", 1)[0])
    except (TypeError, ValueError) as error:
        raise ValueError("protocol version must start with a numeric major") from error


def _public_text(value: object) -> str:
    text = str(value)
    if _SECRET_TEXT.search(text):
        return "[REDACTED]"
    if len(text) > _MAX_PUBLIC_TEXT:
        return text[:_MAX_PUBLIC_TEXT] + "...[TRUNCATED]"
    return text


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else "[INVALID_NUMBER]"
    if isinstance(value, str):
        return _public_text(value)
    if isinstance(value, Mapping):
        clean: dict[str, object] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 128:
                clean["[TRUNCATED]"] = True
                break
            key = _public_text(raw_key)
            if key == "[REDACTED]":
                key = "[REDACTED_KEY]"
            clean[key] = "[REDACTED]" if _sensitive_key(key) else _redact(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth=depth + 1) for item in value[:128]]
    return _public_text(value)


def _evidence(value: Mapping[str, object]) -> Mapping[str, object]:
    clean = _redact(value)
    if not isinstance(clean, dict):
        raise TypeError("redacted evidence must remain an object")
    return MappingProxyType(clean)


def _base_report(
    metadata: ConformanceHostMetadata,
    suites: tuple[str, ...],
    artifact_sha256: str,
    *,
    cases: tuple[ConformanceCaseResult, ...] = (),
    errors: tuple[ConformanceError, ...] = (),
) -> ConformanceReport:
    return ConformanceReport(
        protocol_version=PROTOCOL_VERSION,
        sdk_version=__version__,
        host_name=_public_text(metadata.host_name),
        host_version=_public_text(metadata.host_version),
        platform=platform_module.platform(),
        python_version=platform_module.python_version(),
        artifact_sha256=artifact_sha256,
        suites=suites,
        cases=cases,
        errors=errors,
    )


async def _host(factory: Callable[[], object]) -> ConformanceHost:
    value = factory()
    if inspect.isawaitable(value):
        value = await cast(Awaitable[object], value)
    metadata = getattr(value, "metadata", None)
    if not isinstance(metadata, ConformanceHostMetadata):
        raise TypeError("Host metadata must be ConformanceHostMetadata")
    if not callable(getattr(value, "open_suite", None)):
        raise TypeError("Host must implement open_suite(name)")
    return cast(ConformanceHost, value)


async def run_conformance(
    host_factory: Callable[[], object],
    suites: tuple[str, ...],
    *,
    artifact_sha256: str,
) -> ConformanceReport:
    """Execute all required cases for the selected suites against one Host."""

    selected = validate_suite_names(suites)
    artifact_sha256 = artifact_sha256.strip().lower()
    if not _SHA256.fullmatch(artifact_sha256):
        raise ValueError("artifact_sha256 must be a lowercase SHA-256")
    host = await _host(host_factory)
    metadata = host.metadata
    if _major(metadata.protocol_version) != _major(PROTOCOL_VERSION):
        return _base_report(
            metadata,
            selected,
            artifact_sha256,
            errors=(
                ConformanceError(
                    "protocol_major_mismatch",
                    f"Host protocol major {metadata.protocol_version} is incompatible with SDK {PROTOCOL_VERSION}",
                ),
            ),
        )
    missing = tuple(name for name in selected if name not in metadata.capabilities)
    if missing:
        return _base_report(
            metadata,
            selected,
            artifact_sha256,
            errors=(
                ConformanceError(
                    "missing_capability",
                    f"Host is missing required suites: {', '.join(missing)}",
                ),
            ),
        )

    results: list[ConformanceCaseResult] = []
    errors: list[ConformanceError] = []
    contexts: list[object] = []
    for suite_name in selected:
        try:
            context = host.open_suite(suite_name)
            if any(context is prior for prior in contexts):
                raise RuntimeError("open_suite must return a fresh async context")
            contexts.append(context)
            async with context as suite:
                if not callable(getattr(suite, "aclose", None)):
                    raise TypeError("suite context must expose aclose")
                for definition in CASES_BY_SUITE[suite_name]:
                    started = time.perf_counter()
                    try:
                        operation = getattr(suite, definition.operation, None)
                        if not callable(operation):
                            raise TypeError(
                                f"suite must implement typed operation {definition.operation}"
                            )
                        raw = await operation()
                        if not isinstance(raw, CaseObservation):
                            raise TypeError("typed operation must return CaseObservation")
                        if raw.case_id != definition.case_id:
                            raise ValueError("Host returned a different case identity")
                        verify_observation(raw)
                        status = CaseStatus.PASS
                        message = None
                        evidence = _evidence(cast(Mapping[str, object], raw.evidence))
                    except ConformanceCaseUnavailable as error:
                        del error
                        status = CaseStatus.SKIP
                        message = "Host reported this conformance case unavailable."
                        evidence = MappingProxyType({})
                    except AssertionError as error:
                        del error
                        status = CaseStatus.FAIL
                        message = "SDK verifier rejected the Host observation."
                        evidence = MappingProxyType({})
                    except Exception as error:  # host failures are report facts
                        del error
                        status = CaseStatus.ERROR
                        message = "Host operation failed."
                        evidence = MappingProxyType({})
                    results.append(
                        ConformanceCaseResult(
                            suite=suite_name,
                            case_id=definition.case_id,
                            status=status,
                            required=definition.required,
                            duration_seconds=max(0.0, time.perf_counter() - started),
                            message=message,
                            evidence=cast(Mapping[str, object], evidence),
                        )
                    )
        except Exception as error:  # context enter/exit must fail closed
            del error
            errors.append(
                ConformanceError(
                    "suite_lifecycle_error", "Host suite lifecycle failed.", suite=suite_name
                )
            )
    return _base_report(
        metadata,
        selected,
        artifact_sha256,
        cases=tuple(results),
        errors=tuple(errors),
    )


__all__ = ("PROTOCOL_VERSION", "run_conformance", "validate_suite_names")

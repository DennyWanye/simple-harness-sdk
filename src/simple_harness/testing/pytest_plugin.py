# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pytest fixtures backed by the same runner as the conformance CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from .cli import load_host_factory, parse_host_factory
from .contracts import ConformanceReport
from .runner import PROTOCOL_VERSION, run_conformance, validate_suite_names


def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--simple-harness-host",
        action="store",
        default=None,
        metavar="MODULE:FACTORY",
        help="Host factory specification for conformance tests",
    )
    parser.addoption(
        "--simple-harness-suite",
        action="store",
        default="provider,tool,runtime,workflow",
        metavar="SUITE[,SUITE...]",
        help="Suites executed by simple_harness_conformance_report",
    )
    parser.addoption(
        "--simple-harness-json",
        action="store",
        default=None,
        metavar="PATH",
        help="Optional conformance report output path",
    )
    parser.addoption(
        "--simple-harness-artifact-sha256",
        action="store",
        default=None,
        metavar="SHA256",
        help="Trusted SHA-256 of the exact installed wheel",
    )


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "simple_harness_conformance: mark test as requiring conformance host fixture",
    )


@pytest.fixture(scope="session")
def simple_harness_protocol_version() -> str:
    return PROTOCOL_VERSION


def _factory(request: Any):  # type: ignore[no-untyped-def]
    host_spec = request.config.getoption("--simple-harness-host")
    if not host_spec:
        pytest.skip("Conformance requires --simple-harness-host MODULE:FACTORY")
    try:
        module_name, factory_name = parse_host_factory(host_spec)
        return load_host_factory(module_name, factory_name)
    except (ValueError, ImportError, AttributeError, TypeError) as error:
        raise pytest.UsageError(f"Failed to load conformance host: {error}") from error


@pytest.fixture(scope="session")
def simple_harness_conformance_host(request: Any) -> Any:
    """Compatibility fixture; new consumers should use the report fixture."""

    factory = _factory(request)
    try:
        return factory()
    except Exception as error:
        raise pytest.UsageError("Conformance Host factory failed") from error


@pytest.fixture(scope="session")
def simple_harness_conformance_report(request: Any) -> ConformanceReport:
    factory = _factory(request)
    artifact_sha256 = request.config.getoption("--simple-harness-artifact-sha256")
    if not artifact_sha256:
        raise pytest.UsageError("Conformance requires --simple-harness-artifact-sha256 SHA256")
    raw_suites = request.config.getoption("--simple-harness-suite")
    try:
        suites = validate_suite_names(tuple(item.strip() for item in raw_suites.split(",")))
        report = asyncio.run(run_conformance(factory, suites, artifact_sha256=artifact_sha256))
    except (TypeError, ValueError) as error:
        raise pytest.UsageError(f"Invalid conformance configuration: {error}") from error
    output = request.config.getoption("--simple-harness-json")
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not report.passed:
        pytest.fail("Simple Harness conformance failed", pytrace=False)
    return report


__all__ = (
    "pytest_addoption",
    "pytest_configure",
    "simple_harness_conformance_host",
    "simple_harness_conformance_report",
    "simple_harness_protocol_version",
)

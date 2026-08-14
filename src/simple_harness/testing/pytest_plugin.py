# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Pytest plugin for Simple Harness SDK conformance testing.

This plugin provides fixtures for running conformance tests against
host implementations.

Usage:
    pytest --simple-harness-host module:factory tests/
"""

from __future__ import annotations

from typing import Any

import pytest

from . import PROTOCOL_VERSION
from .cli import load_host_factory, parse_host_factory


def pytest_addoption(parser: Any) -> None:
    """Add command-line options for conformance testing.

    Args:
        parser: pytest parser object
    """
    parser.addoption(
        "--simple-harness-host",
        action="store",
        default=None,
        metavar="MODULE:FACTORY",
        help="Host factory specification for conformance tests (e.g. 'my.module:build_host')",
    )


def pytest_configure(config: Any) -> None:
    """Configure pytest with conformance markers.

    Args:
        config: pytest config object
    """
    config.addinivalue_line(
        "markers",
        "simple_harness_conformance: mark test as requiring conformance host fixture",
    )


@pytest.fixture(scope="session")
def simple_harness_protocol_version() -> str:
    """Protocol version for this SDK release.

    Returns:
        Protocol version string
    """
    return PROTOCOL_VERSION


@pytest.fixture(scope="session")
def simple_harness_conformance_host(request: Any) -> Any:
    """Load and return conformance host implementation.

    This fixture loads the host factory specified via --simple-harness-host
    and returns the host object for use in conformance tests.

    Args:
        request: pytest request object

    Returns:
        Host object from factory

    Raises:
        pytest.UsageError: If --simple-harness-host not provided or invalid
    """
    host_spec = request.config.getoption("--simple-harness-host")
    if not host_spec:
        pytest.skip(
            "Conformance tests require --simple-harness-host MODULE:FACTORY option"
        )

    try:
        module_name, factory_name = parse_host_factory(host_spec)
        factory = load_host_factory(module_name, factory_name)
    except (ValueError, ImportError, AttributeError) as e:
        raise pytest.UsageError(f"Failed to load conformance host: {e}") from e

    # Call factory to get host instance
    try:
        host = factory()
    except Exception as e:
        raise pytest.UsageError(f"Host factory {host_spec!r} failed: {e}") from e

    return host

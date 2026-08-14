# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Conformance testing framework for Simple Harness SDK.

This module provides CLI and pytest plugin interfaces for validating that
host implementations conform to SDK protocols.

Usage:
    # CLI
    python -m simple_harness.testing --host module:factory --suite provider,tool,runtime,workflow --json report.json

    # pytest plugin
    pytest --simple-harness-host module:factory tests/
"""

from __future__ import annotations

__all__ = ["PROTOCOL_VERSION", "run_conformance_suite"]

PROTOCOL_VERSION = "1.0.0"


def run_conformance_suite(
    host_factory: str,
    suites: tuple[str, ...],
    json_output: str | None = None,
) -> int:
    """Run conformance test suites against a host implementation.

    Args:
        host_factory: Module path and factory function (e.g. "my.module:build_host")
        suites: Tuple of suite names to run (provider, tool, runtime, workflow)
        json_output: Optional path to write JSON report

    Returns:
        Exit code (0 for pass, non-zero for failures)
    """
    raise NotImplementedError("T5.4 conformance CLI implementation pending")

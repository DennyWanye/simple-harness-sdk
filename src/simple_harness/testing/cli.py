# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Command-line interface for conformance testing.

Usage:
    python -m simple_harness.testing --host module:factory --suite provider,tool,runtime,workflow --json report.json
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION


def parse_host_factory(spec: str) -> tuple[str, str]:
    """Parse 'module:factory' specification.

    Args:
        spec: String like "my.module:build_host"

    Returns:
        Tuple of (module_name, factory_name)

    Raises:
        ValueError: If spec format is invalid
    """
    if ":" not in spec:
        raise ValueError(
            f"Invalid host factory spec: {spec!r}. Expected format: 'module:factory'"
        )
    module_name, factory_name = spec.split(":", 1)
    if not module_name or not factory_name:
        raise ValueError(
            f"Invalid host factory spec: {spec!r}. Both module and factory must be non-empty"
        )
    return module_name, factory_name


def load_host_factory(module_name: str, factory_name: str) -> Any:
    """Load host factory function from module.

    Args:
        module_name: Python module path
        factory_name: Factory function name

    Returns:
        Factory function

    Raises:
        ImportError: If module cannot be imported
        AttributeError: If factory not found in module
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Cannot import module {module_name!r}: {e}") from e

    try:
        factory = getattr(module, factory_name)
    except AttributeError as e:
        raise AttributeError(
            f"Module {module_name!r} has no attribute {factory_name!r}"
        ) from e

    return factory


def validate_suites(suite_names: list[str]) -> None:
    """Validate suite names.

    Args:
        suite_names: List of requested suite names

    Raises:
        ValueError: If any suite name is invalid
    """
    valid_suites = {"provider", "tool", "runtime", "workflow"}
    invalid = set(suite_names) - valid_suites
    if invalid:
        raise ValueError(
            f"Invalid suite names: {sorted(invalid)}. "
            f"Valid suites: {sorted(valid_suites)}"
        )


def run_conformance_cli(args: list[str] | None = None) -> int:
    """Run conformance CLI.

    Args:
        args: Command-line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    parser = argparse.ArgumentParser(
        prog="python -m simple_harness.testing",
        description="Run Simple Harness SDK conformance tests",
    )
    parser.add_argument(
        "--host",
        required=True,
        metavar="MODULE:FACTORY",
        help="Host factory specification (e.g. 'my.module:build_host')",
    )
    parser.add_argument(
        "--suite",
        required=True,
        metavar="SUITE[,SUITE...]",
        help="Comma-separated list of test suites (provider,tool,runtime,workflow)",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        dest="json_output",
        help="Write JSON report to file",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Simple Harness SDK Testing Framework {PROTOCOL_VERSION}",
    )

    parsed = parser.parse_args(args)

    # Parse and validate inputs
    try:
        module_name, factory_name = parse_host_factory(parsed.host)
        suite_names = [s.strip() for s in parsed.suite.split(",")]
        validate_suites(suite_names)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    # Load host factory
    try:
        factory = load_host_factory(module_name, factory_name)
    except (ImportError, AttributeError) as e:
        print(f"Error loading host factory: {e}", file=sys.stderr)
        return 2

    # Run conformance tests
    print(f"Simple Harness SDK Conformance Testing (protocol {PROTOCOL_VERSION})")
    print(f"Host: {parsed.host}")
    print(f"Suites: {', '.join(suite_names)}")
    print()

    # Placeholder implementation - actual test execution would happen here
    report = {
        "protocol_version": PROTOCOL_VERSION,
        "host": parsed.host,
        "suites": suite_names,
        "status": "not_implemented",
        "message": "T5.4 conformance test execution not yet implemented",
    }

    if parsed.json_output:
        output_path = Path(parsed.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2))
        print(f"Report written to {output_path}")

    print("\nConformance testing framework initialized but test execution pending.")
    print("Full implementation requires T5.4 completion.")
    return 1  # Return 1 to indicate incomplete implementation


def main() -> None:
    """Entry point for CLI."""
    sys.exit(run_conformance_cli())


if __name__ == "__main__":
    main()

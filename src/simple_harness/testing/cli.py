# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Command-line frontend for the shared conformance runner."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any

from .runner import PROTOCOL_VERSION, run_conformance, validate_suite_names


def parse_host_factory(spec: str) -> tuple[str, str]:
    if ":" not in spec:
        raise ValueError(f"Invalid host factory spec: {spec!r}. Expected format: 'module:factory'")
    module_name, factory_name = spec.split(":", 1)
    if not module_name or not factory_name:
        raise ValueError(f"Invalid host factory spec: {spec!r}. Both values must be non-empty")
    return module_name, factory_name


def load_host_factory(module_name: str, factory_name: str) -> Any:
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ImportError(f"Cannot import module {module_name!r}: {error}") from error
    try:
        factory = getattr(module, factory_name)
    except AttributeError as error:
        raise AttributeError(f"Module {module_name!r} has no attribute {factory_name!r}") from error
    if not callable(factory):
        raise TypeError(f"Host factory {module_name}:{factory_name} is not callable")
    return factory


def validate_suites(suite_names: list[str]) -> None:
    try:
        validate_suite_names(tuple(suite_names))
    except ValueError as error:
        raise ValueError(f"Invalid suite names: {error}") from error


def run_conformance_cli(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simple_harness.testing",
        description="Run Simple Harness SDK consumer conformance suites",
    )
    parser.add_argument(
        "--host", required=True, metavar="MODULE:FACTORY", help="Host factory specification"
    )
    parser.add_argument(
        "--suite",
        required=True,
        metavar="SUITE[,SUITE...]",
        help="Comma-separated provider,tool,runtime,workflow,conversation suites",
    )
    parser.add_argument("--json", metavar="PATH", dest="json_output")
    parser.add_argument(
        "--artifact-sha256",
        required=True,
        metavar="SHA256",
        help="Trusted SHA-256 of the exact installed wheel",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Simple Harness SDK conformance protocol {PROTOCOL_VERSION}",
    )
    parsed = parser.parse_args(args)

    try:
        module_name, factory_name = parse_host_factory(parsed.host)
        suites = tuple(item.strip() for item in parsed.suite.split(","))
        validate_suites(list(suites))
        factory = load_host_factory(module_name, factory_name)
    except (ValueError, ImportError, AttributeError, TypeError) as error:
        print(f"Error loading host factory: {error}", file=sys.stderr)
        return 2

    print(f"Simple Harness SDK Conformance Testing (protocol {PROTOCOL_VERSION})")
    print(f"Host: {parsed.host}")
    print(f"Suites: {', '.join(suites)}")
    try:
        report = asyncio.run(
            run_conformance(factory, suites, artifact_sha256=parsed.artifact_sha256)
        )
    except Exception as error:
        print(f"Conformance runner error: {type(error).__name__}", file=sys.stderr)
        return 2

    if parsed.json_output:
        output_path = Path(parsed.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Report written to {output_path}")
    print(f"Result: {'PASS' if report.passed else 'FAIL'}")
    return 0 if report.passed else 1


def main() -> None:
    raise SystemExit(run_conformance_cli())


__all__ = (
    "load_host_factory",
    "main",
    "parse_host_factory",
    "run_conformance_cli",
    "validate_suites",
)


if __name__ == "__main__":
    main()

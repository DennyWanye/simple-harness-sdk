# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Executable consumer conformance protocol for Simple Harness SDK."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .cli import load_host_factory, parse_host_factory
from .contracts import (
    CaseDefinition,
    CaseObservation,
    CaseStatus,
    ConformanceCaseResult,
    ConformanceCaseUnavailable,
    ConformanceError,
    ConformanceHost,
    ConformanceHostMetadata,
    ConformanceReport,
    ConformanceSuite,
    ProviderConformanceSuite,
    RuntimeConformanceSuite,
    ToolConformanceSuite,
    WorkflowConformanceSuite,
)
from .runner import PROTOCOL_VERSION, run_conformance
from .sinks import NoopDeliverySink


def run_conformance_suite(
    host_factory: str,
    suites: tuple[str, ...],
    artifact_sha256: str,
    json_output: str | None = None,
) -> int:
    """Compatibility entrypoint used by embedded consumers."""

    module_name, factory_name = parse_host_factory(host_factory)
    factory = load_host_factory(module_name, factory_name)
    report = asyncio.run(
        run_conformance(factory, suites, artifact_sha256=artifact_sha256)
    )
    if json_output is not None:
        path = Path(json_output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report.to_json(), indent=2) + "\n", encoding="utf-8")
    return 0 if report.passed else 1


__all__ = (
    "PROTOCOL_VERSION",
    "CaseDefinition",
    "CaseObservation",
    "CaseStatus",
    "ConformanceCaseUnavailable",
    "ConformanceCaseResult",
    "ConformanceError",
    "ConformanceHost",
    "ConformanceHostMetadata",
    "ConformanceReport",
    "ConformanceSuite",
    "ProviderConformanceSuite",
    "RuntimeConformanceSuite",
    "ToolConformanceSuite",
    "WorkflowConformanceSuite",
    "NoopDeliverySink",
    "run_conformance",
    "run_conformance_suite",
)

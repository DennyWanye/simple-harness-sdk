# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Versioned checks: external is a recorded phase, never a seventh Task layer.

Source coverage cannot establish truth or issue a PASS. Its default explanation is
deterministic; the decision seam also permits a trusted adapter to request a person.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from ..contracts import ContractError
from ..contracts.models import sha256_hex
from .evidence_resolver import EvidenceResolver, SourceRead

if TYPE_CHECKING:
    from .assessments import AssessmentBindingV1

CHECK_SPECS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        "code_test@v1": MappingProxyType(
            {"layer": "code_test", "phase": "execution", "activation": "policy"}
        ),
        "citation_integrity@v2": MappingProxyType(
            {"layer": "rule_check", "phase": "deterministic", "activation": "required"}
        ),
        "source_coverage@v1": MappingProxyType(
            {"layer": "rule_check", "phase": "external", "activation": "on_inconclusive"}
        ),
    }
)
DOCUMENT_CHECKS = ("citation_integrity@v2", "source_coverage@v1")
COVERAGE_VERDICTS = frozenset({"FAIL", "INCONCLUSIVE", "NEEDS_HUMAN"})


def coverage_verdict(
    *, sources: Sequence[SourceRead], eligible: Sequence[Mapping[str, Any]]
) -> str:
    """No interpretation of source instructions can improve deterministic eligibility."""
    if not sources or not eligible:
        raise ContractError("source coverage requires resolved sources and eligible candidates")
    return "INCONCLUSIVE"


def _inputs(binding: AssessmentBindingV1, eligible: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    versions: dict[str, str] = {}
    for row in eligible:
        if row.get("verdict") != "INCONCLUSIVE":
            raise ContractError("external coverage received an ineligible assessment")
        for path, version in row["source_versions"].items():
            if binding.source_versions.get(path) != version:
                raise ContractError("external source differs from the frozen version")
            versions[path] = version
    return {
        "schema": 1,
        "binding_hash": binding.binding_hash,
        "assessment_receipts": sorted(row["receipt_id"] for row in eligible),
        "source_versions": dict(sorted(versions.items())),
    }


def coverage_record(
    *, binding: AssessmentBindingV1, eligible: Sequence[Mapping[str, Any]], verdict: str | None
) -> dict[str, Any]:
    if eligible and verdict not in COVERAGE_VERDICTS:
        raise ContractError("source_coverage may only return FAIL, INCONCLUSIVE or NEEDS_HUMAN")
    if not eligible and verdict is not None:
        raise ContractError("an unexecuted coverage check has no verdict")
    inputs = _inputs(binding, eligible)
    reasons = {
        "FAIL": ["external_coverage_failed"],
        "INCONCLUSIVE": ["resolved_sources_do_not_establish_deterministic_binding"],
        "NEEDS_HUMAN": ["external_coverage_requires_human_review"],
    }
    result = {
        "adapter_id": "source_coverage",
        "version": "1",
        "phase": "external",
        "execution": "COMPLETED" if eligible else "NOT_APPLICABLE",
        "verdict": verdict,
        "input_hash": sha256_hex(inputs),
        "source_versions": inputs["source_versions"],
        "reasons": reasons.get(verdict or "", []),
    }
    result["receipt_id"] = "coverage-" + sha256_hex(result)
    return result


def source_coverage(
    *,
    binding: AssessmentBindingV1,
    eligible: Sequence[Mapping[str, Any]],
    resolver: EvidenceResolver,
) -> dict[str, Any]:
    inputs = _inputs(binding, eligible)
    if not eligible:
        raise ContractError("ineligible external coverage must not be dispatched")
    sources = []
    for path, version in inputs["source_versions"].items():
        source = resolver.read_source(
            tenant_id=binding.tenant_id,
            mission_id=binding.mission_id,
            path=path,
            version=version,
            source_roots=binding.source_roots,
        )
        if source.status != "resolved":
            raise ContractError(f"external source unavailable: {source.status}")
        sources.append(source)
    verdict = coverage_verdict(sources=tuple(sources), eligible=eligible)
    return coverage_record(binding=binding, eligible=eligible, verdict=verdict)


__all__ = ("CHECK_SPECS", "DOCUMENT_CHECKS", "coverage_verdict", "source_coverage")

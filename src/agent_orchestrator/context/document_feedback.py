# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""Repair diagnostics without duplicating assessment envelopes/source blocks.

The immutable verification record remains the authority. Required current goals,
contracts, source identities and criterion catalogs never pass through this view.
"""

from collections.abc import Mapping
from typing import Any

from ..contracts.models import sha256_hex

_SCALARS = frozenset(
    {
        "attempt_id",
        "result_id",
        "claim_id",
        "criterion_id",
        "receipt_id",
        "layer",
        "status",
        "verdict",
        "reason",
        "code",
        "error_code",
        "error_type",
        "kind",
        "scope",
        "path",
        "version",
        "citation_index",
        "requested",
        "remaining",
        "request_tokens",
        "budget_tokens",
        "required_over_budget",
        "source_kind",
        "target",
        "source_version",
        "start_line",
        "end_line",
    }
)
_LISTS = frozenset({"hard_failures", "problems", "reasons", "claim_ids"})
_NESTED = frozenset(
    {
        "failure",
        "failures",
        "detail",
        "error",
        "criterion_verdicts",
        "limitations_check",
        "required",
        "missing",
        "provided",
        "structural_result",
        "citations",
        "resolution",
        "ref",
        "locator",
    }
)


def document_repair_feedback(value: Mapping[str, Any]) -> dict[str, Any]:
    def project(item: Any) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                if key in _SCALARS and isinstance(child, (str, int, float, bool, type(None))):
                    result[key] = child
                elif key in _LISTS and isinstance(child, (list, tuple)):
                    result[key] = [project(x) for x in child]
                elif key in _NESTED and isinstance(child, (Mapping, list, tuple)):
                    result[key] = project(child)
            return result
        if isinstance(item, (list, tuple)):
            return [project(x) for x in item]
        return item if isinstance(item, (str, int, float, bool, type(None))) else None

    return {
        "schema": "document-repair-feedback-v1",
        "data_not_instruction": True,
        "record_sha256": sha256_hex(dict(value)),
        "record_prose_not_inlined": True,
        "diagnostic": project(value),
        "repair_guidance": (
            "Use original current criterion IDs and source versions. Missing limitations "
            "identify each required claim/criterion pair; provide its actual evidence gap. "
            "Do not remove required criterion bindings or turn an inference into a quotation. "
            "Citation diagnostics identify the original claim, citation index, source version "
            "and line range. For quote_not_whole_unit, re-read and quote the complete source "
            "unit including its conditions; preserve literal punctuation and quotation marks. "
            "Read registered source files again when needed; a prior FAIL is not evidence."
        ),
    }

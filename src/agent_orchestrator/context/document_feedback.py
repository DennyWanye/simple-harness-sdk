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


# 2026-09-26 真机文档任务: a repair Worker saw only the bare code
# "no_content_binding_or_candidate" and resubmitted the same unlinked claims twice.
# Each hard-failure code now carries one concrete instruction naming the fields to fix.
_CODE_HINTS = {
    "no_content_binding_or_candidate": (
        "No claim is linked to criterion {criteria}. For a free-text criterion, add a claim "
        "whose content is exactly that criterion's text in doc_assessment.criteria, with "
        "citations quoted from sources: that literal binding can pass. Putting the id in "
        "criterion_ids (never together with criterion_refs) is only a candidate link, "
        "which stays inconclusive and needs a limitations item."
    ),
    "missing_citation": (
        "A claim linked to criterion {criteria} has no citations; add citations with "
        "path, version, start_line, end_line and a verbatim quote from the source."
    ),
    "citation_not_resolved": (
        "A citation could not be located in its source; re-read the source and quote the "
        "cited lines verbatim with the current version."
    ),
    "missing_limitations": (
        "Each pair in limitations_check.missing needs a top-level limitations item "
        "{{criterion_id, claim_id, missing}} describing the actual evidence gap."
    ),
}


def _hard_failures(item: Any) -> list[tuple[str, list[str]]]:
    """(code, failing criterion ids) for every hard failure in the record."""
    found: list[tuple[str, list[str]]] = []
    if isinstance(item, Mapping):
        codes = item.get("hard_failures")
        if isinstance(codes, (list, tuple)):
            verdicts = item.get("criterion_verdicts") or []
            for code in codes:
                criteria = [
                    str(v.get("criterion_id")) for v in verdicts
                    if isinstance(v, Mapping) and code in (v.get("reasons") or [])
                ]
                found.append((str(code), criteria))
        for child in item.values():
            found.extend(_hard_failures(child))
    elif isinstance(item, (list, tuple)):
        for child in item:
            found.extend(_hard_failures(child))
    return found


def repair_hints(value: Mapping[str, Any]) -> list[str]:
    hints: list[str] = []
    for code, criteria in _hard_failures(value):
        template = _CODE_HINTS.get(code)
        if template is None:
            continue
        hint = template.format(criteria=", ".join(criteria) or "(see criterion_verdicts)")
        if hint not in hints:
            hints.append(hint)
    return hints


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

    feedback = {
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
    hints = repair_hints(value)
    if hints:
        feedback["repair_hints"] = hints
    return feedback

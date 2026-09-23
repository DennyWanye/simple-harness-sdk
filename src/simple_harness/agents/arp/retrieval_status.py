# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0

"""Context-free retrieval truth table (CONTEXT-SEARCH C5, R2).

``status_for`` is the single status decision: an incomplete scan or coverage
outranks degradation, which outranks zero hits.  The validators enforce the
receipt / page / aggregate / summary consistency that every producer and every
nested Host decoder must call.  Source and ACL truth are never simulated here.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from .errors import ArpError
from .strict import digest

Json = Mapping[str, object]


def need(value: object, code: str = "RETRIEVAL_STATE_INVALID") -> None:
    if not value:
        raise ArpError(code)


def status_for(coverage: Json, query_result_count: int | None) -> str:
    if coverage["phase"] == "SCANNING":
        return "PARTIAL"
    if coverage["index_coverage"] == "PARTIAL":
        return "PARTIAL"
    if coverage["mode"] == "LEXICAL_ONLY":
        return "LEXICAL_ONLY"
    return "COMPLETE_EMPTY" if query_result_count == 0 else "COMPLETE"


def validate_coverage(c: Json) -> None:
    need(0 <= c["scanned_chunks"] <= c["snapshot_chunks"])  # type: ignore[operator]
    need(0 <= c["vector_ready_groups"] <= c["indexed_groups"] <= c["expected_groups"])  # type: ignore[operator]
    if c["phase"] == "SCANNING":
        need(not c["ranking_final"] and c["rank_scope"] == "NONE")
    else:
        need(c["ranking_final"] and c["rank_scope"] == "FROZEN_INDEX_CHANNEL_TOPK")
        need(c["scanned_chunks"] == c["snapshot_chunks"])
    if c["index_coverage"] == "COMPLETE":
        need(c["indexed_groups"] == c["expected_groups"])
        if c["mode"] == "HYBRID":
            need(c["vector_ready_groups"] == c["expected_groups"])


def validate_receipt(r: Json) -> None:
    coverage = r["coverage"]
    validate_coverage(coverage)  # type: ignore[arg-type]
    total = r["query_result_count"]
    count = r["returned_count"]
    offset = r["page_offset"]
    need(r["indexed_closed_groups"] == coverage["indexed_groups"])  # type: ignore[index]
    need(0 <= r["searched_closed_groups"] <= coverage["indexed_groups"])  # type: ignore[index, operator]
    if coverage["phase"] == "SCANNING":  # type: ignore[index]
        need(total is None and count == 0 and offset == 0 and r["has_more"])
    else:
        need(total is not None and 0 <= offset <= total and offset + count <= total)  # type: ignore[operator]
        need(r["searched_closed_groups"] == coverage["indexed_groups"])  # type: ignore[index]
        if total == 0:
            need(offset == 0 and count == 0 and not r["has_more"])
        else:
            need(offset < total and count > 0)  # type: ignore[operator]
            need(r["has_more"] == (offset + count < total))  # type: ignore[operator]
    need(r["status"] == status_for(coverage, total))  # type: ignore[arg-type]


def validate_page(p: Json) -> None:
    receipt = p["receipt"]
    validate_receipt(receipt)  # type: ignore[arg-type]
    need(p["has_more"] == receipt["has_more"] == (p["next_cursor"] is not None))  # type: ignore[index]
    items: Sequence[Json] = p["items"]  # type: ignore[assignment]
    need(receipt["returned_count"] == len(items))  # type: ignore[index]
    coverage = receipt["coverage"]  # type: ignore[index]
    if coverage["phase"] == "SCANNING":
        need(not items and p["page_semantics"] == "PROGRESS")
    else:
        need(p["page_semantics"] == "APPEND_FINAL")
        offset = receipt["page_offset"]  # type: ignore[index]
        need(
            [x["rank_ordinal"] for x in items]
            == list(range(offset + 1, offset + 1 + len(items)))  # type: ignore[operator]
        )
        need(len({x["chunk_id"] for x in items}) == len(items))


def validate_aggregate(r: Json) -> None:
    coverage = r["coverage"]
    items: Sequence[Json] = r["candidate_items"]  # type: ignore[assignment]
    total = r["query_result_count"]
    need(r["candidate_set_hash"] == digest(items), "RECALL_AGGREGATE_MISMATCH")
    need(r["page_chain_hash"] == digest(r["page_refs"]), "RECALL_AGGREGATE_MISMATCH")
    page_refs: Sequence[Json] = r["page_refs"]  # type: ignore[assignment]
    need(
        len({(p["kind"], p["id"], p["revision"], p["content_hash"]) for p in page_refs})
        == len(page_refs)
    )
    if r["outcome"] == "READY":
        need(r["skip_code"] is None and coverage is not None and coverage["phase"] == "RESULTS")  # type: ignore[index]
        validate_coverage(coverage)  # type: ignore[arg-type]
        need(r["index_snapshot_ref"] is not None and r["search_query_id"] is not None and page_refs)
        need(total == len(items))
        need([x["rank_ordinal"] for x in items] == list(range(1, len(items) + 1)))
        need(len({x["chunk_id"] for x in items}) == len(items))
        need(r["status"] == status_for(coverage, total))  # type: ignore[arg-type]
    else:
        need(not items and total is None and r["skip_code"] is not None)
        if r["skip_code"] in ("EMPTY_QUERY", "RECALL_DISABLED"):
            need(r["status"] == "NOT_REQUESTED" and coverage is None and not page_refs)
            need(r["index_snapshot_ref"] is None and r["search_query_id"] is None)
            need(r["query_embedding_invocation_ref"] is None and not r["unsettled_call_refs"])
        else:
            need(r["status"] == "PARTIAL")
            if coverage is not None:
                validate_coverage(coverage)  # type: ignore[arg-type]


def validate_summary(s: Json) -> None:
    if s["outcome"] == "READY":
        coverage = s["coverage"]
        need(coverage is not None and coverage["phase"] == "RESULTS" and s["skip_code"] is None)  # type: ignore[index]
        validate_coverage(coverage)  # type: ignore[arg-type]
        need(s["query_result_count"] == s["candidate_count"])
        need(0 <= s["selected_count"] <= s["candidate_count"])  # type: ignore[operator]
        need(s["status"] == status_for(coverage, s["query_result_count"]))  # type: ignore[arg-type]
    else:
        need(s["query_result_count"] is None and s["candidate_count"] == 0 and s["selected_count"] == 0)
        need(s["skip_code"] is not None)
        expected = "NOT_REQUESTED" if s["skip_code"] in ("EMPTY_QUERY", "RECALL_DISABLED") else "PARTIAL"
        need(s["status"] == expected)
        if s["coverage"] is not None:
            validate_coverage(s["coverage"])  # type: ignore[arg-type]


__all__ = (
    "status_for",
    "validate_aggregate",
    "validate_coverage",
    "validate_page",
    "validate_receipt",
    "validate_summary",
)

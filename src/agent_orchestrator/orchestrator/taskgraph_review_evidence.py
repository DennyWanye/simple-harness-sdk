# SPDX-License-Identifier: Apache-2.0
"""Bounded original verification facts for a TaskGraph root review.

These are observations about an accepted result, never authority to accept the
root. Read the immutable VerificationPassed event, not mutable latest reports.
"""
from __future__ import annotations

import json
from typing import Any

from ..contracts.models import VERIFICATION_LAYERS
from ..contracts.resolution import ReviewVerdict
from ..contracts.semantic_base import content_hash_of
from ..storage.store import StoreError


def accepted_verification_evidence(store: Any, semantics: Any, mission_id: str,
                                   acceptance: Any) -> dict[str, Any]:
    review = semantics.get_review_record(str(acceptance.review_record_id))
    package = semantics.get_review_package(str(review.record.package_id))
    binding = package.binding
    if (not review.official or review.record.verdict != ReviewVerdict.ACCEPT
            or review.record.binding != binding
            or review.record.purpose != package.purpose
            or str(acceptance.mission_id) != mission_id
            or str(binding.mission_id) != mission_id
            or str(binding.subject_ref.kind) != "task"
            or str(binding.subject_ref.id) != str(acceptance.task_id)
            or binding.subject_ref.revision != acceptance.contract_revision
            or binding.obligation_id != acceptance.obligation_id
            or binding.requirements_revision != acceptance.requirements_revision
            or binding.input_manifest_hash != acceptance.input_manifest_hash):
        raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_IDENTITY_MISMATCH")
    candidates = []
    for ref in package.candidate_refs:
        result = store.get_result(str(ref.id))
        if result is not None:
            if ref.content_hash != content_hash_of(str(result.envelope.id)):
                raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_RESULT_MISMATCH")
            if (str(package.purpose) != "TASK_CONTENT"
                    or result.verification_state != "DONE" or result.verdict != "PASS"):
                raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_RESULT_NOT_ACCEPTED")
            candidates.append(result.envelope)
    if not candidates:
        return {"status": "UNAVAILABLE", "reason": "contribution_has_no_result_candidate"}
    if len(candidates) != 1:
        raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_RESULT_AMBIGUOUS")
    result = candidates[0]
    if result.mission_id != mission_id or result.task_id != str(acceptance.task_id):
        raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_RESULT_MISMATCH")
    rows = store.connection.execute(
        "SELECT event_id,seq,payload_json FROM events WHERE mission_id=? AND task_id=? "
        "AND attempt_id=? AND type='VerificationPassed' "
        "AND json_extract(payload_json,'$.result_id')=? ORDER BY seq",
        (mission_id, result.task_id, result.attempt_id, result.id)).fetchall()
    if not rows:
        return {"status": "UNAVAILABLE", "reason": "original_verification_event_missing"}
    if len(rows) != 1:
        raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_EVENT_AMBIGUOUS")
    row = rows[0]
    try:
        payload = json.loads(row[2])
        layers = _bounded_layers(payload, mission_id, result)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_MALFORMED_EVENT") from exc
    return {"status": "RECORDED", "result_id": result.id, "attempt_id": result.attempt_id,
            "source_event": {"id": row[0], "seq": row[1], "payload_hash": content_hash_of(payload)},
            "layers": layers, "scope": "Original child verification only; independent root review is still required."}


def _bounded_layers(payload: dict[str, Any], mission_id: str, result: Any) -> list[dict[str, Any]]:
    raw_layers = payload["layers"]
    if not isinstance(raw_layers, list) or not 0 < len(raw_layers) <= len(VERIFICATION_LAYERS):
        raise ValueError("invalid layers")
    layers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for layer in raw_layers:
        name = layer["layer"]
        if name not in VERIFICATION_LAYERS or name in seen:
            raise ValueError("unknown or duplicate layer")
        seen.add(name)
        item = {"layer": name, "status": _text(layer["status"], 32)}
        summary = layer.get("summary", "")
        item.update(summary=_text(summary, 1024), summary_truncated=len(summary) > 1024)
        if name == "code_test":
            detail = layer.get("detail", {})
            scope = detail.get("observation_scope")
            if scope is not None:
                if any(scope.get(key) != value for key, value in {
                        "mission_id": mission_id, "task_id": result.task_id,
                        "attempt_id": result.attempt_id, "result_id": result.id}.items()):
                    raise StoreError("TASKGRAPH_REVIEW_EVIDENCE_SCOPE_MISMATCH")
                item["observation_scope"] = {key: _text(scope.get(key), 256) for key in
                    ("schema", "mission_id", "task_id", "attempt_id", "result_id", "workspace_hash", "checker")}
                item["observation_scope"].update(
                    criteria_count=len(scope.get("criteria", [])),
                    artifact_count=len(scope.get("artifact_hashes", {})),
                    projection="identities_and_counts_only")
            runs = detail.get("runs", [])
            if not isinstance(runs, list):
                raise ValueError("invalid runs")
            if runs and scope is None:
                layers.append({"layer": name, "evidence_status": "UNBOUND",
                               "run_count": len(runs),
                               "summary": "Original test observations lack a result-bound scope; no test verdict is supplied."})
                continue
            item.update(run_count=len(runs), runs_truncated=len(runs) > 16, runs=[])
            for run in runs[:16]:
                receipt = run.get("receipt", {})
                target = run.get("target", "")
                item["runs"].append({"target": _text(target, 512), "target_truncated": isinstance(target, str) and len(target) > 512,
                    "passed": _scalar(run.get("passed")), "returncode": _scalar(run.get("returncode")),
                    "timed_out": _scalar(run.get("timed_out")),
                    "receipt": {key: _scalar(receipt.get(key)) for key in
                        ("execution_id", "status", "exit_code", "isolated", "timed_out", "environment_digest")}})
        layers.append(item)
    return layers


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value[:limit]


def _scalar(value: Any) -> str | int | bool | None:
    # Receipt identity/digest/status fields are small protocol values; reject
    # oversized identities rather than displaying a misleading truncated ID.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and -(2**63) <= value < 2**63:
        return value
    if isinstance(value, str) and len(value) <= 256:
        return value
    raise ValueError("invalid receipt scalar")

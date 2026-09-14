# SPDX-FileCopyrightText: 2026 DennyWanye
# SPDX-License-Identifier: Apache-2.0
"""System-authored observations: exact check, result, input hashes and outcome.

These assert only that pytest passed on a particular snapshot, never that an
arbitrary model statement is entailed by that result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import Artifact, Claim, ClaimStatus, ResultEnvelope, Task
from ..contracts.models import sha256_hex
from .verified_knowledge import KnowledgeRecord


def scoped_test_observations(
    envelope: ResultEnvelope,
    artifacts: Sequence[Artifact],
    layers: Sequence[Mapping[str, Any]],
    now: float,
    task: Task,
) -> list[tuple[Claim, KnowledgeRecord]]:
    expected = {
        "schema": "pytest-observation-v2",
        "mission_id": envelope.mission_id,
        "task_id": envelope.task_id,
        "attempt_id": envelope.attempt_id,
        "result_id": envelope.id,
        "artifact_hashes": {a.path: a.content_hash for a in artifacts},
        "checker": "pytest",
        "criteria": list(task.success_criteria),
    }
    observations: list[tuple[Claim, KnowledgeRecord]] = []
    for layer in layers:
        if layer.get("layer") != "code_test" or layer.get("status") != "PASS":
            continue
        detail = layer.get("detail")
        if not isinstance(detail, Mapping):
            continue
        scope = detail.get("observation_scope")
        if not isinstance(scope, Mapping) or any(scope.get(k) != v for k, v in expected.items()):
            continue
        workspace_hash = scope.get("workspace_hash")
        if (
            not isinstance(workspace_hash, str)
            or len(workspace_hash) != 64
            or any(c not in "0123456789abcdef" for c in workspace_hash)
        ):
            continue
        runs = detail.get("runs", [])
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, Mapping) or run.get("passed") is not True:
                continue
            receipt = run.get("receipt")
            if (
                run.get("returncode") != 0
                or run.get("timed_out") is not False
                or not isinstance(receipt, Mapping)
                or receipt.get("status") != "ok"
                or not receipt.get("execution_id")
                or receipt.get("exit_code") != 0
                or receipt.get("timed_out") is not False
            ):
                continue
            target = run.get("target")
            if not isinstance(target, str) or not target.strip():
                continue  # No scope inference from a whole-tree check.
            basis = {
                "layer": "code_test",
                "system_observation": True,
                "scope": dict(scope),
                "target": target,
                "run_hash": sha256_hex(dict(run)),
            }
            identity = "observation:" + sha256_hex(basis)
            content = (
                f"pytest target {target!r} passed on workspace SHA-256 {workspace_hash} "
                f"for result {envelope.id}. This observation establishes only that test outcome."
            )
            evidence = ("pytest:" + target,)
            claim = Claim(
                id=identity,
                content=content,
                type="test_observation",
                status=ClaimStatus.VERIFIED,
                source_task=envelope.task_id,
                source_attempt=envelope.attempt_id,
                evidence=evidence,
                dependencies=envelope.used_knowledge,
                verifier_results=(basis,),
                confidence_metadata={"grade": "verified", "system": True},
                supersedes=None,
                mission_id=envelope.mission_id,
                result_id=envelope.id,
                proposed_by="system:pytest-observation-v2",
            )
            record = KnowledgeRecord(
                id=identity,
                mission_id=envelope.mission_id,
                claim_id=identity,
                content=content,
                type=claim.type,
                status="VERIFIED",
                version=1,
                key=None,
                stance="affirms",
                proposed_by=claim.proposed_by,
                source_task=envelope.task_id,
                source_attempt=envelope.attempt_id,
                source_result=envelope.id,
                evidence=evidence,
                verifier=basis,
                dependencies=envelope.used_knowledge,
                created_at=now,
                evidence_trust=("trusted",),
            )
            observations.append((claim, record))
    return observations
